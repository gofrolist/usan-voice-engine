import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallbackRequest


async def create_callback_request(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    requested_time_text: str,
    requested_at: datetime | None,
    notes: str | None,
) -> CallbackRequest:
    row = CallbackRequest(
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_callback_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[CallbackRequest]:
    """Most-recent callback requests, optionally filtered by status. Bounded by limit."""
    stmt = select(CallbackRequest)
    if status is not None:
        stmt = stmt.where(CallbackRequest.status == status)
    stmt = stmt.order_by(CallbackRequest.created_at.desc(), CallbackRequest.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
