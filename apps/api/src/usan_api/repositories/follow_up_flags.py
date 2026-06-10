import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import FollowUpFlag

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
    return row


async def list_flags(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[FollowUpFlag]:
    """Most-recent flags, optionally filtered by status/elder (newest first)."""
    limit = max(1, min(limit, MAX_FLAGS_LIMIT))
    stmt = select(FollowUpFlag)
    if status is not None:
        stmt = stmt.where(FollowUpFlag.status == status)
    if elder_id is not None:
        stmt = stmt.where(FollowUpFlag.elder_id == elder_id)
    stmt = stmt.order_by(FollowUpFlag.created_at.desc(), FollowUpFlag.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


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
