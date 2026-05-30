import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/calls", tags=["calls"])


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
    """Persist a queued call, dispatch it, and transition to dialing."""
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
        # Lost a concurrent race on the unique idempotency_key: the row now
        # exists. Re-fetch and apply the same replay contract (200 or 409).
        await db.rollback()
        existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is None:
            raise HTTPException(status_code=409, detail="idempotency_key conflict") from exc
        return _idempotent_replay(existing, body, response)

    try:
        await livekit_dispatch.dispatch_outbound_call(call, elder=elder, settings=settings)
    except livekit_dispatch.OutboundDispatchError as exc:
        # Keep the operational reason in the DB error column + logs; don't leak
        # internal config var names to the (currently unauthenticated) caller.
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
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Outbound dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    dialing = await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched")
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


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(call_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return CallResponse.from_model(call)
