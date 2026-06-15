import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import MedicationLog


async def create_medication_log(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID,
    medication_name: str,
    taken: bool,
    reported_time: datetime | None,
) -> MedicationLog:
    row = MedicationLog(
        call_id=call_id,
        contact_id=contact_id,
        medication_name=medication_name,
        taken=taken,
        reported_time=reported_time,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
