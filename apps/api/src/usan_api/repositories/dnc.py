from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import DNCEntry


async def is_blocked(db: AsyncSession, phone_e164: str) -> bool:
    result = await db.execute(select(DNCEntry).where(DNCEntry.phone_e164 == phone_e164))
    return result.scalar_one_or_none() is not None


async def add_entry(db: AsyncSession, phone_e164: str, reason: str | None) -> DNCEntry:
    entry = await db.get(DNCEntry, phone_e164)
    if entry is None:
        entry = DNCEntry(phone_e164=phone_e164, reason=reason)
        db.add(entry)
    else:
        entry.reason = reason
    await db.flush()
    await db.refresh(entry)
    return entry


async def remove_entry(db: AsyncSession, phone_e164: str) -> bool:
    entry = await db.get(DNCEntry, phone_e164)
    if entry is None:
        return False
    await db.delete(entry)
    await db.flush()
    return True
