import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import dialer, livekit_dispatch, object_storage
from usan_api.auth import require_service_token, require_worker_token
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder, WellnessLog
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.call import (
    CallOutcomeRequest,
    CallResponse,
    CreateCallRequest,
    InboundCallRequest,
    InboundCallResponse,
)
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/calls", tags=["calls"])


def _format_last_check_in(log: WellnessLog) -> str:
    """A short human summary of the elder's most recent wellness log."""
    parts = [f"on {log.logged_at.date().isoformat()}"]
    if log.mood is not None:
        parts.append(f"mood {log.mood}/5")
    if log.pain_level is not None:
        parts.append(f"pain {log.pain_level}/10")
    summary = ", ".join(parts)
    if log.notes:
        summary += f" — note: {log.notes}"
    return summary


def _idempotent_replay(existing: Call, body: CreateCallRequest, response: Response) -> CallResponse:
    """Return the existing call for a replayed key (200), or 409 on payload conflict."""
    if existing.elder_id != body.elder_id or existing.dynamic_vars != body.dynamic_vars:
        raise HTTPException(
            status_code=409, detail="idempotency_key reused with a different payload"
        )
    response.status_code = status.HTTP_200_OK
    return CallResponse.from_model(existing)


async def _create_and_dispatch(
    db: AsyncSession,
    body: CreateCallRequest,
    elder: Elder,
    settings: Settings,
    response: Response,
) -> CallResponse:
    """Persist a queued call, dispatch the agent, schedule the background dial."""
    room = f"usan-outbound-{uuid.uuid4()}"
    try:
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            idempotency_key=body.idempotency_key,
            livekit_room=room,
            dynamic_vars=body.dynamic_vars,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is None:
            raise HTTPException(status_code=409, detail="idempotency_key conflict") from exc
        return _idempotent_replay(existing, body, response)

    try:
        await livekit_dispatch.dispatch_agent(call, settings=settings)
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(db, call.id, CallStatus.FAILED, error={"reason": str(exc)})
        await db.commit()
        raise HTTPException(status_code=503, detail="outbound calling is not available") from exc
    except Exception as exc:
        await calls_repo.set_status(
            db,
            call.id,
            CallStatus.FAILED,
            error={"reason": "dispatch_error", "exc_type": type(exc).__name__},
        )
        await calls_repo.schedule_retry(db, call.id)
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Agent dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    dialing = await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    dialer.schedule_dial(call.id, settings)
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched; dialing")
    return CallResponse.from_model(dialing or call)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CallResponse)
async def enqueue_call(
    body: CreateCallRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    elder = await elders_repo.get_elder(db, body.elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="elder not found")

    # Serialize the DNC-check-and-create window against a concurrent add_dnc (and
    # duplicate enqueues) for the same number. Released at the commit below.
    await dnc_repo.lock_phone(db, elder.phone_e164)

    existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
    if existing is not None:
        return _idempotent_replay(existing, body, response)

    if await dnc_repo.is_blocked(db, elder.phone_e164):
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
            idempotency_key=body.idempotency_key,
            dynamic_vars=body.dynamic_vars,
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).info("Outbound call blocked by DNC")
        response.status_code = status.HTTP_200_OK
        return CallResponse.from_model(call)

    return await _create_and_dispatch(db, body, elder, settings, response)


@router.post("/inbound", response_model=InboundCallResponse)
async def register_inbound_call(
    body: InboundCallRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> InboundCallResponse:
    """Register an answered inbound call and return per-elder dynamic vars.

    Called by the agent worker once an inbound SIP caller is present (spec §3
    step 3). Looks the caller up by phone; an unknown/absent number still gets a
    call record (elder_id NULL). Never checks DNC — DNC governs outbound only.
    """
    elder = await elders_repo.get_elder_by_phone(db, body.phone_e164) if body.phone_e164 else None
    dynamic_vars: dict[str, Any] = {}
    if elder is not None:
        dynamic_vars["elder_name"] = elder.name
        last = await wellness_repo.get_latest_for_elder(db, elder.id)
        if last is not None:
            dynamic_vars["last_check_in"] = _format_last_check_in(last)
    call = await calls_repo.create_inbound_call(
        db,
        elder_id=elder.id if elder is not None else None,
        livekit_room=body.livekit_room,
        sip_call_id=body.sip_call_id,
        dynamic_vars=dynamic_vars,
    )
    await db.commit()
    logger.bind(call_id=str(call.id), elder_known=elder is not None).info("Inbound call registered")
    return InboundCallResponse(
        call_id=call.id, elder_known=elder is not None, dynamic_vars=dynamic_vars
    )


async def _presigned_recording_url(call: Call, settings: Settings) -> str | None:
    """Sign a short-lived GET URL for the call's recording, or None if absent/disabled."""
    if not call.recording_uri or not settings.gcs_bucket:
        return None
    try:
        url = await asyncio.to_thread(
            object_storage.generate_signed_url,
            call.recording_uri,
            settings.recording_signed_url_ttl_s,
        )
    except Exception:
        logger.bind(call_id=str(call.id)).warning("Failed to sign recording URL")
        return None
    # Access log: every issued recording URL is recorded (spec §10).
    logger.bind(call_id=str(call.id), recording_uri=call.recording_uri).info(
        "Recording URL accessed"
    )
    return url


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    presigned = await _presigned_recording_url(call, settings)
    return CallResponse.from_model(call, presigned_recording_url=presigned)


@router.post("/{call_id}/outcome", response_model=CallResponse)
async def report_outcome(
    call_id: uuid.UUID,
    body: CallOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallResponse:
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    # body.outcome is constrained to "voicemail_left"; gate on in_progress so a
    # late/duplicate report never overrides an already-terminal call. The mark and
    # its retry share ONE commit so a crash can't leave a terminal call un-retried.
    updated = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
    if updated is not None:
        await calls_repo.schedule_retry(db, call_id)
    await db.commit()
    logger.bind(call_id=str(call_id)).info("Call outcome reported: {o}", o=body.outcome)
    return CallResponse.from_model(updated or call)
