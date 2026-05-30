from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import DNCEntry

# Transaction-scoped advisory lock keyed on a phone number. The literal 4476483
# (0x444E43, "DNC") namespaces the lock keyspace; hashtext() maps the phone to
# the second int4 key. Taken by both the call-enqueue gate and add_dnc so a
# number cannot be added to the DNC list between the gate check and the dial.
# Released automatically on commit/rollback.
_PHONE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(4476483, hashtext(:phone))")


async def lock_phone(db: AsyncSession, phone_e164: str) -> None:
    """Serialize concurrent DNC changes and call enqueues for one phone number."""
    await db.execute(_PHONE_LOCK_SQL, {"phone": phone_e164})


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
