import uuid
from typing import Any

from sqlalchemy import Result, case, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_events
from usan_api.db.models import Contact, FollowUpFlag
from usan_api.repositories import webhook_outbox

# Bound the list read: flags accumulate per call/contact over time. Default cap
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
    contact_id: uuid.UUID,
    severity: str,
    category: str,
    reason: str | None,
) -> FollowUpFlag:
    row = FollowUpFlag(
        call_id=call_id,
        contact_id=contact_id,
        severity=severity,
        category=category,
        reason=reason,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    # flag.created joins this same transaction (transactional outbox, spec
    # §2.1): the caller's existing commit makes flag and event durable
    # together. Payload deliberately reduced per spec §6.4 — NO contact_id, NO
    # category, NO reason — even though this row carries all three.
    await webhook_outbox.enqueue_event(
        db, event="flag.created", payload=webhook_events.flag_created_payload(row)
    )
    return row


async def list_flags(
    db: AsyncSession,
    *,
    status: str | None = None,
    contact_id: uuid.UUID | None = None,
    severity: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[FollowUpFlag, str | None, str | None]]:
    """Flags + contact name/phone via outerjoin (admin read model, spec §4.4).

    Urgent-first ordering — `(severity='urgent') DESC, created_at DESC, id DESC`
    — so an urgent flag older than a page of routine flags is never invisible.
    NOT served by idx_followup_flags_status: acceptable at current volumes;
    revisit with a partial index if flags ever number in the tens of thousands.
    """
    limit = max(1, min(limit, MAX_FLAGS_LIMIT))
    offset = max(0, offset)
    stmt = select(FollowUpFlag, Contact.name, Contact.phone_e164).outerjoin(
        Contact, FollowUpFlag.contact_id == Contact.id
    )
    if status is not None:
        stmt = stmt.where(FollowUpFlag.status == status)
    if contact_id is not None:
        stmt = stmt.where(FollowUpFlag.contact_id == contact_id)
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


async def upsert_crisis_flag(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID,
    crisis_category: str,
    detection_source: str,
    resource_offered: str,
) -> tuple[FollowUpFlag, bool]:
    """Upsert an urgent crisis flag, idempotent per (call_id, crisis_category).

    Returns ``(flag, created)``. On conflict (the other detection path already flagged
    this category for the call), detection_source is merged to 'both' when it differs,
    and resource_offered is filled if it was NULL. The ON CONFLICT target is the partial
    unique index uq_followup_crisis, so the upsert is atomic and race-safe even if the
    LLM and the safety net escalate the same category near-simultaneously. ``created`` is
    derived from the Postgres ``xmax = 0`` insert-vs-update signal. Mirrors
    create_follow_up_flag by enqueueing the PHI-safe flag.created webhook ONLY on first
    creation. Flush-only; the caller commits.
    """
    insert_stmt = pg_insert(FollowUpFlag).values(
        call_id=call_id,
        contact_id=contact_id,
        severity="urgent",
        category="safety",
        status="open",
        crisis_category=crisis_category,
        detection_source=detection_source,
        resource_offered=resource_offered,
    )
    # 'both' when the existing row's source differs from this one (LLM + safety net).
    merged_source = case(
        (
            FollowUpFlag.detection_source == insert_stmt.excluded.detection_source,
            FollowUpFlag.detection_source,
        ),
        else_="both",
    )
    result: Result[Any] = await db.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=[FollowUpFlag.call_id, FollowUpFlag.crisis_category],
            index_where=FollowUpFlag.crisis_category.isnot(None),
            set_={
                "detection_source": merged_source,
                "resource_offered": func.coalesce(
                    FollowUpFlag.resource_offered, insert_stmt.excluded.resource_offered
                ),
            },
        ).returning(FollowUpFlag.id, literal_column("(xmax = 0)"))
    )
    flag_id, created = result.one()
    await db.flush()
    # Re-read fresh (populate_existing) so the merged columns + server-default created_at
    # are current even if a prior read left a stale identity-map copy.
    row = (
        await db.execute(
            select(FollowUpFlag)
            .where(FollowUpFlag.id == flag_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if created:
        # Mirror create_follow_up_flag: the PHI-safe flag.created event joins this txn.
        # Payload deliberately omits contact_id/category/reason (spec §6.4).
        await webhook_outbox.enqueue_event(
            db, event="flag.created", payload=webhook_events.flag_created_payload(row)
        )
    return row, bool(created)


async def mark_family_notified(db: AsyncSession, flag_id: int) -> None:
    """Mark that a family alert was enqueued for this crisis flag (idempotent set).

    Flush-only; the caller commits.
    """
    await db.execute(
        update(FollowUpFlag).where(FollowUpFlag.id == flag_id).values(family_notified=True)
    )


# Operator-queue surfacing text (FR-013). PHI-safe (no clinical detail) — these are
# shown to operators in the admin follow-up-flags queue, never sent over SMS.
_NO_FAMILY_CRISIS_REASON = "No family contact registered — operator action required (FR-013)."
_MISSED_NO_FAMILY_REASON = (
    "Missed wellness call and no family contact registered to alert (FR-010 / FR-013)."
)
_OPT_OUT_REASON = "Contact opted out of calls during a conversation (US7 / FR-039)."


async def set_no_family_contact_reason(db: AsyncSession, flag_id: int) -> None:
    """Surface on a crisis flag that there was no family contact to notify (FR-013).

    Idempotent (fixed PHI-safe text). The urgent flag itself is the operator-queue entry;
    this annotates *why* no family was alerted. Flush-only; the caller commits.
    """
    await db.execute(
        update(FollowUpFlag)
        .where(FollowUpFlag.id == flag_id)
        .values(reason=_NO_FAMILY_CRISIS_REASON)
    )


async def ensure_operator_missed_flag(
    db: AsyncSession, *, call_id: uuid.UUID, contact_id: uuid.UUID
) -> FollowUpFlag:
    """Idempotent operator-queue entry for a missed call with no family contact (FR-013).

    At most one ``operator_alert`` (routine) flag per call — a re-entry of the finalizer
    must not pile up duplicates. Returns the existing flag if present, else creates one
    (which also enqueues the PHI-safe flag.created webhook). Flush-only; the caller commits.
    """
    existing = await db.execute(
        select(FollowUpFlag).where(
            FollowUpFlag.call_id == call_id,
            FollowUpFlag.category == "operator_alert",
        )
    )
    row = existing.scalars().first()
    if row is not None:
        return row
    return await create_follow_up_flag(
        db,
        call_id=call_id,
        contact_id=contact_id,
        severity="routine",
        category="operator_alert",
        reason=_MISSED_NO_FAMILY_REASON,
    )


async def ensure_opt_out_flag(
    db: AsyncSession, *, call_id: uuid.UUID, contact_id: uuid.UUID
) -> FollowUpFlag:
    """Idempotent operator-queue entry recording a spoken opt-out (US7 / FR-039).

    At most one ``operator_alert`` (routine) flag per call — a re-invocation of
    ``register_opt_out`` in the same call must not pile up duplicates (a connected call
    has no competing missed-call operator flag). Returns the existing flag if present,
    else creates one (which also enqueues the PHI-safe flag.created webhook). Flush-only;
    the caller commits.
    """
    existing = await db.execute(
        select(FollowUpFlag).where(
            FollowUpFlag.call_id == call_id,
            FollowUpFlag.category == "operator_alert",
        )
    )
    row = existing.scalars().first()
    if row is not None:
        return row
    return await create_follow_up_flag(
        db,
        call_id=call_id,
        contact_id=contact_id,
        severity="routine",
        category="operator_alert",
        reason=_OPT_OUT_REASON,
    )
