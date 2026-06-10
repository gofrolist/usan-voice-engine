import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallbackRequest

# Bound the list read: callback requests accumulate per call/elder over time.
# Default cap mirrors the sibling follow_up_flags repo (MAX_FLAGS_LIMIT=500);
# newest-first so the cap keeps the most recent.
MAX_CALLBACKS_LIMIT = 500

# Legal queue transitions, keyed by target status. The WHERE clause of
# update_status IS the state machine: a status not listed as a predecessor of
# the target makes the guarded UPDATE touch zero rows.
_ALLOWED_PREDECESSORS: dict[str, tuple[str, ...]] = {
    "acknowledged": ("open",),
    "resolved": ("open", "acknowledged"),
}


async def create_callback_request(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    requested_time_text: str,
    requested_at: datetime | None,
    notes: str | None,
) -> CallbackRequest:
    row = CallbackRequest(
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_callback_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[CallbackRequest]:
    """Most-recent callback requests, optionally filtered by status/elder (newest first)."""
    limit = max(1, min(limit, MAX_CALLBACKS_LIMIT))
    stmt = select(CallbackRequest)
    if status is not None:
        stmt = stmt.where(CallbackRequest.status == status)
    if elder_id is not None:
        stmt = stmt.where(CallbackRequest.elder_id == elder_id)
    stmt = stmt.order_by(CallbackRequest.created_at.desc(), CallbackRequest.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_request(db: AsyncSession, request_id: int) -> CallbackRequest | None:
    # populate_existing: callers re-read after a zero-row guarded UPDATE to
    # disambiguate no-op vs 409 — the answer must come from the database, not a
    # stale identity-map object left over from their pre-read.
    stmt = (
        select(CallbackRequest)
        .where(CallbackRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_status(
    db: AsyncSession,
    request_id: int,
    *,
    new_status: str,
    actor_email: str,
) -> CallbackRequest | None:
    """Single status-guarded UPDATE — the WHERE clause IS the state machine.

    No read-modify-write race. Zero rows updated returns None; the caller
    disambiguates 404 / idempotent no-op / 409 via get_request(). Flush-only;
    the router commits.
    """
    stmt = (
        update(CallbackRequest)
        .where(
            CallbackRequest.id == request_id,
            CallbackRequest.status.in_(_ALLOWED_PREDECESSORS[new_status]),
        )
        .values(
            status=new_status,
            status_updated_at=func.now(),
            status_updated_by=actor_email,
        )
        .returning(CallbackRequest)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def count_by_status(db: AsyncSession) -> dict[str, int]:
    """Counts per status (GROUP BY, served by idx_callback_requests_status).

    Absent statuses are omitted from the dict, not reported as 0.
    """
    stmt = select(CallbackRequest.status, func.count()).group_by(CallbackRequest.status)
    result = await db.execute(stmt)
    return {status: count for status, count in result.all()}
