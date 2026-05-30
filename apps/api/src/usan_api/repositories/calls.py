import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


async def mark_answered(
    db: AsyncSession, call_id: uuid.UUID, *, sip_call_id: str | None
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = CallStatus.IN_PROGRESS
    call.answered_at = _utcnow()
    if sip_call_id:
        call.sip_call_id = sip_call_id
    await db.flush()
    await db.refresh(call)
    return call


async def mark_dial_failure(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    end_reason: str,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call


async def mark_completed_if_in_progress(db: AsyncSession, livekit_room: str) -> Call | None:
    # livekit_room is not UNIQUE at the schema level (room names are uuid4 so a
    # collision is astronomically unlikely); take the most recent match rather
    # than scalar_one_or_none(), which would 500 on the impossible duplicate.
    result = await db.execute(
        select(Call)
        .where(Call.livekit_room == livekit_room)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    call = result.scalars().first()
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = "hangup"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call
