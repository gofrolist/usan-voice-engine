import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_events
from usan_api.db.base import CallStatus
from usan_api.db.models import Call, CallbackRequest, Elder
from usan_api.repositories import webhook_outbox

# The callback dialer (US8) records who advanced the row; admin actors use their email.
_DIALER_ACTOR = "system:callback_dialer"

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
    profile_override: uuid.UUID | None = None,
) -> CallbackRequest:
    row = CallbackRequest(
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
        profile_override=profile_override,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    # callback.created joins this same transaction (transactional outbox, spec
    # §2.1). Payload carries the parsed requested_at only — never
    # requested_time_text/notes (spec §6.5); elder_id stays (no health content,
    # the §6.4 line is the health-information x person-identifier pairing).
    await webhook_outbox.enqueue_event(
        db, event="callback.created", payload=webhook_events.callback_created_payload(row)
    )
    return row


async def list_callback_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[CallbackRequest, str | None, str | None]]:
    """Callback requests + elder name/phone via outerjoin (admin read model, §4.4).

    Newest first (ordering unchanged by C3 — no urgent-first here).
    """
    limit = max(1, min(limit, MAX_CALLBACKS_LIMIT))
    offset = max(0, offset)
    stmt = select(CallbackRequest, Elder.name, Elder.phone_e164).outerjoin(
        Elder, CallbackRequest.elder_id == Elder.id
    )
    if status is not None:
        stmt = stmt.where(CallbackRequest.status == status)
    if elder_id is not None:
        stmt = stmt.where(CallbackRequest.elder_id == elder_id)
    stmt = (
        stmt.order_by(CallbackRequest.created_at.desc(), CallbackRequest.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


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


# ── Callback auto-dial (US8 / T074) ──────────────────────────────────────────
# A separate, system-driven lane from the open→acknowledged→resolved ops queue: the
# dialer materializes a Call for a due request and advances open→scheduled→dialed.


async def list_due_open_ids(db: AsyncSession, *, now: datetime, limit: int) -> list[int]:
    """IDs of ``open`` callbacks whose parsed time has arrived (oldest first).

    Only requests with a resolved ``requested_at`` are auto-dialed; one with a NULL time
    the agent could not parse stays in the ops queue for a human (FR-030). This is a plain
    read (no lock); the dialer re-locks each row by id when it materializes, so a stale id
    that another worker already took is skipped there.
    """
    stmt = (
        select(CallbackRequest.id)
        .where(
            CallbackRequest.status == "open",
            CallbackRequest.requested_at.is_not(None),
            CallbackRequest.requested_at <= now,
        )
        .order_by(CallbackRequest.requested_at)
        .limit(max(1, limit))
    )
    return list((await db.execute(stmt)).scalars())


async def claim_open_for_dial(db: AsyncSession, request_id: int) -> CallbackRequest | None:
    """Row-lock one still-``open`` callback for materialization (FOR UPDATE).

    Returns None if another worker already advanced it past ``open`` — the caller then
    skips it (SKIP LOCKED semantics across the per-request transaction).
    """
    stmt = (
        select(CallbackRequest)
        .where(CallbackRequest.id == request_id, CallbackRequest.status == "open")
        .with_for_update()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def mark_scheduled(
    db: AsyncSession, request_id: int, *, dispatched_call_id: uuid.UUID
) -> CallbackRequest | None:
    """Advance ``open -> scheduled`` and link the materialized Call (guarded UPDATE)."""
    stmt = (
        update(CallbackRequest)
        .where(CallbackRequest.id == request_id, CallbackRequest.status == "open")
        .values(
            status="scheduled",
            dispatched_call_id=dispatched_call_id,
            status_updated_at=func.now(),
            status_updated_by=_DIALER_ACTOR,
        )
        .returning(CallbackRequest)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def reconcile_dialed(db: AsyncSession) -> int:
    """Advance ``scheduled -> dialed`` for callbacks whose Call has left the queue.

    A dispatched Call leaves QUEUED when the scheduler picks it up (DIALING/…) or it was
    terminal at birth (DNC_BLOCKED). Either way the callback is done from the dialer's
    side. Bulk guarded UPDATE; returns the number advanced.
    """
    left_queue = select(Call.id).where(Call.status != CallStatus.QUEUED)
    stmt = (
        update(CallbackRequest)
        .where(
            CallbackRequest.status == "scheduled",
            CallbackRequest.dispatched_call_id.in_(left_queue),
        )
        .values(
            status="dialed",
            status_updated_at=func.now(),
            status_updated_by=_DIALER_ACTOR,
        )
        .returning(CallbackRequest.id)
    )
    return len((await db.execute(stmt)).scalars().all())
