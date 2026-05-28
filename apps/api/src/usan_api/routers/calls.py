import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/calls", tags=["calls"])


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

    existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
    if existing is not None:
        if existing.elder_id != body.elder_id or existing.dynamic_vars != body.dynamic_vars:
            raise HTTPException(
                status_code=409,
                detail="idempotency_key reused with a different payload",
            )
        response.status_code = status.HTTP_200_OK
        return CallResponse.from_model(existing)

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

    room = f"usan-outbound-{uuid.uuid4()}"
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

    try:
        await livekit_dispatch.dispatch_outbound_call(call, elder=elder, settings=settings)
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(db, call.id, CallStatus.FAILED, error={"reason": str(exc)})
        await db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        await calls_repo.set_status(
            db, call.id, CallStatus.FAILED, error={"reason": "dispatch_error"}
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Outbound dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched")
    return CallResponse.from_model(call)


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(call_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return CallResponse.from_model(call)
