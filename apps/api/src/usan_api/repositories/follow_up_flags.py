import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_events
from usan_api.db.models import Elder, FollowUpFlag
from usan_api.repositories import webhook_outbox

# Bound the list read: flags accumulate per call/elder over time. Default cap
# mirrors the audit/agent-profiles repos (_MAX_LIST_LIMIT=500); newest-first so
# the cap keeps the most recent.
MAX_FLAGS_LIMIT = 500

# Legal queue transitions, keyed by target status. The WHERE clause of
# update_status IS the state machine: a status not listed as a predecessor of
# the target makes the guarded UPDATE touch zero rows.
_ALLOWED_PREDECESSORS: dict[str, tuple[str, ...]] = {
    "acknowledged": ("open",),
    "resolved": ("open", "acknowledged"),
}


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
    # flag.created joins this same transaction (transactional outbox, spec
    # §2.1): the caller's existing commit makes flag and event durable
    # together. Payload deliberately reduced per spec §6.4 — NO elder_id, NO
    # category, NO reason — even though this row carries all three.
    await webhook_outbox.enqueue_event(
        db, event="flag.created", payload=webhook_events.flag_created_payload(row)
    )
    return row


async def list_flags(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    severity: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[FollowUpFlag, str | None, str | None]]:
    """Flags + elder name/phone via outerjoin (admin read model, spec §4.4).

    Urgent-first ordering — `(severity='urgent') DESC, created_at DESC, id DESC`
    — so an urgent flag older than a page of routine flags is never invisible.
    NOT served by idx_followup_flags_status: acceptable at current volumes;
    revisit with a partial index if flags ever number in the tens of thousands.
    """
    limit = max(1, min(limit, MAX_FLAGS_LIMIT))
    offset = max(0, offset)
    stmt = select(FollowUpFlag, Elder.name, Elder.phone_e164).outerjoin(
        Elder, FollowUpFlag.elder_id == Elder.id
    )
    if status is not None:
        stmt = stmt.where(FollowUpFlag.status == status)
    if elder_id is not None:
        stmt = stmt.where(FollowUpFlag.elder_id == elder_id)
    if severity is not None:
        stmt = stmt.where(FollowUpFlag.severity == severity)
    stmt = (
        stmt.order_by(
            (FollowUpFlag.severity == "urgent").desc(),
            FollowUpFlag.created_at.desc(),
            FollowUpFlag.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


async def get_flag(db: AsyncSession, flag_id: int) -> FollowUpFlag | None:
    # populate_existing: callers re-read after a zero-row guarded UPDATE to
    # disambiguate no-op vs 409 — the answer must come from the database, not a
    # stale identity-map object left over from their pre-read.
    stmt = (
        select(FollowUpFlag)
        .where(FollowUpFlag.id == flag_id)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_status(
    db: AsyncSession,
    flag_id: int,
    *,
    new_status: str,
    actor_email: str,
) -> FollowUpFlag | None:
    """Single status-guarded UPDATE — the WHERE clause IS the state machine.

    No read-modify-write race. Zero rows updated returns None; the caller
    disambiguates 404 / idempotent no-op / 409 via get_flag(). Flush-only;
    the router commits.
    """
    stmt = (
        update(FollowUpFlag)
        .where(
            FollowUpFlag.id == flag_id,
            FollowUpFlag.status.in_(_ALLOWED_PREDECESSORS[new_status]),
        )
        .values(
            status=new_status,
            status_updated_at=func.now(),
            status_updated_by=actor_email,
        )
        .returning(FollowUpFlag)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def count_by_status(db: AsyncSession) -> dict[str, int]:
    """Counts per status (GROUP BY, served by idx_followup_flags_status).

    Absent statuses are omitted from the dict, not reported as 0.
    """
    stmt = select(FollowUpFlag.status, func.count()).group_by(FollowUpFlag.status)
    result = await db.execute(stmt)
    return {status: count for status, count in result.all()}


async def count_open_urgent(db: AsyncSession) -> int:
    """Open urgent flags (status='open' AND severity='urgent')."""
    stmt = (
        select(func.count())
        .select_from(FollowUpFlag)
        .where(FollowUpFlag.status == "open", FollowUpFlag.severity == "urgent")
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())
