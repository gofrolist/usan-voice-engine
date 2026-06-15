import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WellnessLog


async def create_wellness_log(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID,
    mood: int | None,
    pain_level: int | None,
    notes: str | None,
) -> WellnessLog:
    row = WellnessLog(
        call_id=call_id,
        contact_id=contact_id,
        mood=mood,
        pain_level=pain_level,
        notes=notes,
        raw={},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_latest_for_contact(db: AsyncSession, contact_id: uuid.UUID) -> WellnessLog | None:
    """The contact's most recent wellness log (for the inbound 'last check-in' var).

    Ordered by logged_at then id descending so a tie on logged_at (rows written in
    one transaction share now()) resolves deterministically to the newest insert.
    """
    result = await db.execute(
        select(WellnessLog)
        .where(WellnessLog.contact_id == contact_id)
        .order_by(WellnessLog.logged_at.desc(), WellnessLog.id.desc())
        .limit(1)
    )
    return result.scalars().first()
