import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call


async def create_call(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    direction: CallDirection,
    status: CallStatus,
    idempotency_key: str | None = None,
    livekit_room: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    call = Call(
        elder_id=elder_id,
        direction=direction,
        status=status,
        idempotency_key=idempotency_key,
        livekit_room=livekit_room,
        dynamic_vars=dynamic_vars or {},
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    return call


async def get_call(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    return await db.get(Call, call_id)


async def get_by_idempotency_key(db: AsyncSession, key: str) -> Call | None:
    result = await db.execute(select(Call).where(Call.idempotency_key == key))
    return result.scalar_one_or_none()


async def set_status(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call
