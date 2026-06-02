import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WellnessLog


async def create_wellness_log(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    mood: int | None,
    pain_level: int | None,
    notes: str | None,
) -> WellnessLog:
    row = WellnessLog(
        call_id=call_id,
        elder_id=elder_id,
        mood=mood,
        pain_level=pain_level,
        notes=notes,
        raw={},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_latest_for_elder(db: AsyncSession, elder_id: uuid.UUID) -> WellnessLog | None:
    """The elder's most recent wellness log (for the inbound 'last check-in' var).

    Ordered by logged_at then id descending so a tie on logged_at (rows written in
    one transaction share now()) resolves deterministically to the newest insert.
    """
    result = await db.execute(
        select(WellnessLog)
        .where(WellnessLog.elder_id == elder_id)
        .order_by(WellnessLog.logged_at.desc(), WellnessLog.id.desc())
        .limit(1)
    )
    return result.scalars().first()
