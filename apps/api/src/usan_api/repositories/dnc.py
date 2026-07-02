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


async def _get_for_org(db: AsyncSession, phone_e164: str) -> DNCEntry | None:
    # RLS scopes this to the current org, so a phone another org already suppressed
    # is invisible here. db.get() can't be used now that the PK is composite
    # (phone_e164, organization_id) and organization_id is RLS-injected, not known here.
    result = await db.execute(select(DNCEntry).where(DNCEntry.phone_e164 == phone_e164))
    return result.scalar_one_or_none()


async def add_entry(db: AsyncSession, phone_e164: str, reason: str | None) -> DNCEntry:
    entry = await _get_for_org(db, phone_e164)
    if entry is None:
        entry = DNCEntry(phone_e164=phone_e164, reason=reason)
        db.add(entry)
    else:
        entry.reason = reason
    await db.flush()
    await db.refresh(entry)
    return entry


async def remove_entry(db: AsyncSession, phone_e164: str) -> bool:
    entry = await _get_for_org(db, phone_e164)
    if entry is None:
        return False
    await db.delete(entry)
    await db.flush()
    return True


async def list_entries(db: AsyncSession, *, limit: int = 200, offset: int = 0) -> list[DNCEntry]:
    """Newest-first page of DNC entries for the current org (RLS-scoped)."""
    result = await db.execute(
        select(DNCEntry).order_by(DNCEntry.added_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())
