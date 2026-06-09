import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import FollowUpFlag


async def create_follow_up_flag(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    severity: str,
    category: str,
    reason: str | None,
) -> FollowUpFlag:
    row = FollowUpFlag(
        call_id=call_id,
        elder_id=elder_id,
        severity=severity,
        category=category,
        reason=reason,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_flags(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[FollowUpFlag]:
    """Most-recent flags, optionally filtered by status/elder (newest first)."""
    stmt = select(FollowUpFlag)
    if status is not None:
        stmt = stmt.where(FollowUpFlag.status == status)
    if elder_id is not None:
        stmt = stmt.where(FollowUpFlag.elder_id == elder_id)
    stmt = stmt.order_by(FollowUpFlag.created_at.desc(), FollowUpFlag.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
