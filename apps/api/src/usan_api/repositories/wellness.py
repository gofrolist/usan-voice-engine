import uuid

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
