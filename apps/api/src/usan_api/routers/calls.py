import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import phi_audit, recording_urls
from usan_api.auth import require_operator_token, require_service_token, require_worker_token
from usan_api.builtin_vars import build_memory_params, resolve_builtin_vars
from usan_api.builtin_vars import format_last_check_in as _format_last_check_in
from usan_api.client_ip import client_ip
from usan_api.compat import ids, inbound_router
from usan_api.compat.errors import CompatError
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.phone import to_e164
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import conversation_summaries as conversation_summaries_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.repositories import medication_reminders as medication_reminders_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import survey_results as survey_results_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.call import (
    CallOutcomeRequest,
    CallResponse,
    CreateCallRequest,
    InboundCallRequest,
    InboundCallResponse,
)
from usan_api.services import outbound_calls
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/calls", tags=["calls"])


def _idempotent_replay(existing: Call, body: CreateCallRequest, response: Response) -> CallResponse:
    """Return the existing call for a replayed key (200), or 409 on payload conflict."""
    if (
        existing.contact_id != body.contact_id
        or existing.dynamic_vars != body.dynamic_vars
        or existing.profile_override != body.profile_override
    ):
        raise HTTPException(
            status_code=409, detail="idempotency_key reused with a different payload"
        )
    response.status_code = status.HTTP_200_OK
    return CallResponse.from_model(existing)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CallResponse,
    dependencies=[Depends(require_operator_token)],
)
async def enqueue_call(
    body: CreateCallRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    contact = await contacts_repo.get_contact(db, body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")

    # Serialize the DNC-check-and-create window against a concurrent add_dnc (and
    # duplicate enqueues) for the same number. Released at the commit below.
    await dnc_repo.lock_phone(db, contact.phone_e164)

    existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
    if existing is not None:
        return _idempotent_replay(existing, body, response)

    # Liveness runs on the create path only, AFTER the replay pre-check (ordering
    # contract, spec §3.1): an identical replay must return the original call even
    # when the override profile was archived since — that is the retry-on-timeout
    # contract idempotency keys exist for. One check covers both create branches
    # (dispatch + DNC). Auth tier: operator-token scope; the validation is identical
    # to the admin-session schedules/batches gates and grants no new authority (§7).
    if body.profile_override is not None:
        await outbound_calls.require_live_override(db, body.profile_override)

    if await dnc_repo.is_blocked(db, contact.phone_e164):
        call = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
            idempotency_key=body.idempotency_key,
            dynamic_vars=body.dynamic_vars,
            profile_override=body.profile_override,
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).info("Outbound call blocked by DNC")
        response.status_code = status.HTTP_200_OK
        return CallResponse.from_model(call)

    try:
        return await outbound_calls.create_and_dispatch(
            db, body=body, contact=contact, settings=settings
        )
    except IntegrityError as exc:
        await db.rollback()
        existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is None:
            raise HTTPException(status_code=409, detail="idempotency_key conflict") from exc
        return _idempotent_replay(existing, body, response)


async def _resolve_inbound_override(
    db: AsyncSession,
    settings: Settings,
    *,
    from_number: str | None,
    to_number: str | None,
    dynamic_vars: dict[str, Any],
) -> uuid.UUID | None:
    """Surface 2A: ask the client's router who to be; return the override profile or None.

    On any router failure OR an override_agent_id we can't resolve to a published voice profile,
    returns None so the caller degrades to the DID's default inbound agent (spec §3). On success,
    merges the router's dynamic_variables OVER ``dynamic_vars`` (the client's CRM is source of
    truth) and returns the profile UUID to store as the call's profile_override.
    """
    if not settings.compat_inbound_router_enabled:
        return None
    result = await inbound_router.route_inbound(
        settings, from_number=from_number, to_number=to_number
    )
    if result is None:
        return None
    try:
        profile_id = ids.decode_agent_id(result.override_agent_id)
    except CompatError:
        logger.warning("inbound router returned an undecodable override_agent_id; degrading")
        return None
    if not await agent_profiles_repo.is_live_profile(db, profile_id, channel="voice"):
        logger.bind(profile_id=str(profile_id)).warning(
            "inbound router override is not a published voice agent; degrading"
        )
        return None
    # Router vars win over same-keyed contact vars — the client's CRM is authoritative (spec §3).
    dynamic_vars.update(result.dynamic_variables)
    return profile_id


@router.post("/inbound", response_model=InboundCallResponse)
async def register_inbound_call(
    body: InboundCallRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> InboundCallResponse:
    """Register an answered inbound call and return per-contact dynamic vars.

    Called by the agent worker once an inbound SIP caller is present (spec §3
    step 3). Looks the caller up by phone; an unknown/absent number still gets a
    call record (contact_id NULL). Never checks DNC — DNC governs outbound only.
    """
    # Normalize the caller-ID to E.164 before lookup: Telnyx delivers it as a bare
    # US national number (e.g. "6692388604"), but contacts are stored E.164
    # ("+16692388604"), so the raw value would never match. See usan_api.phone.
    phone = to_e164(body.phone_e164)
    contact = await contacts_repo.get_contact_by_phone(db, phone) if phone else None
    # dynamic_vars stays the caller/operator-supplied dict (idempotency payload, §4.3);
    # legacy single-brace slots remain for old inbound templates. Built-ins go into
    # resolved_vars, NOT here.
    dynamic_vars: dict[str, Any] = {}
    last = None
    open_task_messages: list[str] = []
    pending_med_names: list[str] = []
    # US6 / FR-032: survey_due stays False for an unknown caller; computed once contact known.
    survey_due = False
    # Memory built-ins default empty for an unknown caller; populated once the contact is known.
    memory: dict[str, Any] = build_memory_params([], None, timezone="", now=datetime.now(UTC))
    if contact is not None:
        dynamic_vars["contact_name"] = contact.name
        last = await wellness_repo.get_latest_for_contact(db, contact.id)
        if last is not None:
            dynamic_vars["last_check_in"] = _format_last_check_in(last)
        open_tasks = await family_tasks_repo.list_open_family_tasks(db, contact_id=contact.id)
        open_task_messages = [t.message for t in open_tasks]
        pending_meds = await medication_reminders_repo.list_pending(db, contact_id=contact.id)
        pending_med_names = [r.medication_name for r in pending_meds]
        facts = await personal_facts_repo.list_active(db, contact_id=contact.id)
        summary = await conversation_summaries_repo.get_latest(db, contact_id=contact.id)
        memory = build_memory_params(
            facts, summary, timezone=contact.timezone or "", now=datetime.now(UTC)
        )
        # US6 / FR-032: due when the contact has no survey for this (local) month yet.
        period_month = survey_results_repo.month_anchor(contact.timezone or "", datetime.now(UTC))
        survey_due = not await survey_results_repo.exists_for_month(
            db, contact_id=contact.id, period_month=period_month
        )
    resolved_vars, timezone = resolve_builtin_vars(
        contact,
        last,
        direction="inbound",
        open_family_tasks=open_task_messages,
        pending_med_reasks=pending_med_names,
        survey_due=survey_due,
        **memory,
    )
    # Surface 2A: when the inbound-call-router is on, let the client pick the agent + vars. On any
    # failure this returns None (profile_override stays unset), degrading to the default inbound
    # agent — the call still connects. Merges router vars into dynamic_vars in place.
    profile_override = await _resolve_inbound_override(
        db,
        settings,
        from_number=phone,
        to_number=to_e164(body.to_number),
        dynamic_vars=dynamic_vars,
    )
    call = await calls_repo.create_inbound_call(
        db,
        contact_id=contact.id if contact is not None else None,
        livekit_room=body.livekit_room,
        sip_call_id=body.sip_call_id,
        dynamic_vars=dynamic_vars,
        profile_override=profile_override,
    )
    await db.commit()
    logger.bind(
        call_id=str(call.id),
        contact_known=contact is not None,
        override_applied=profile_override is not None,
    ).info("Inbound call registered")
    return InboundCallResponse(
        call_id=call.id,
        contact_known=contact is not None,
        dynamic_vars=dynamic_vars,
        resolved_vars=resolved_vars,
        timezone=timezone,
        override_applied=profile_override is not None,
    )


@router.get(
    "/{call_id}",
    response_model=CallResponse,
    dependencies=[Depends(require_operator_token)],
)
async def get_call(
    call_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    # Real client IP (X-Forwarded-For first hop behind Caddy), so the PHI access
    # audit trail names the operator's host rather than the proxy container.
    client_host = client_ip(request)
    presigned = await recording_urls.presigned_recording_url(
        call, settings, client_host=client_host
    )
    transcript = await transcripts_repo.list_for_call(db, call_id)
    if transcript:
        # PHI access audit (spec §10): a returned transcript exposes PHI, so the
        # access is logged like the recording path. Only the segment count and the
        # caller's host are recorded — never the transcript content itself.
        phi_audit.log_transcript_accessed(
            call_id=call_id, client=client_host, segments=len(transcript)
        )
    return CallResponse.from_model(call, presigned_recording_url=presigned, transcript=transcript)


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
