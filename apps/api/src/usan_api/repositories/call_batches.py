"""Repository for `call_batches` + `call_batch_targets` (one-off campaigns).

House rules: functions take the request session, `flush()` (+`refresh()` for
returned rows), and never commit — routers and the scheduler poller own the
transaction boundary.

Target transitions are status-guarded (spec §3.3 lifecycle:
``pending → materialized → done``, ``pending → skipped``, batch cancel flips
``pending → cancelled``): a stale claim or a cancel race can never resurrect a
settled row — the guards return ``False`` instead of writing.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, time

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from usan_api.db.models import CallBatch, CallBatchTarget
from usan_api.schemas.batch import BatchTargetIn

# Bound the list read (house pattern: sibling repos cap at 500); newest-first
# with an id tiebreaker so the cap keeps the most recent (spec §8 bounded reads).
MAX_BATCHES_LIMIT = 500

# CHECK ck_call_batch_targets_status — `target_counts` zero-fills all five.
TARGET_STATUSES = ("pending", "materialized", "done", "skipped", "cancelled")
# idx_call_batches_open / idx_call_batch_targets_open predicates, verbatim.
OPEN_BATCH_STATUSES = ("running", "cancelled")
OPEN_TARGET_STATUSES = ("pending", "materialized")


async def create_batch_with_targets(
    db: AsyncSession,
    *,
    name: str,
    idempotency_key: str | None,
    payload_digest: str,
    trigger_at: datetime | None,
    window_start_local: time | None,
    window_end_local: time | None,
    days_of_week: int | None,
    max_concurrency: int | None,
    profile_override: uuid.UUID | None,
    targets: Sequence[BatchTargetIn],
) -> CallBatch:
    """Insert the batch plus every target in ONE flush (no commit).

    The batch id is minted client-side so the targets can reference it without
    an extra flush round-trip; ``target_index`` is the submitted array position
    (spec §3.3). UNIQUE ``idempotency_key`` raises IntegrityError on a replayed
    key — the router resolves replay-vs-conflict via ``payload_digest``.
    """
    batch = CallBatch(
        id=uuid.uuid4(),
        name=name,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        trigger_at=trigger_at,
        window_start_local=window_start_local,
        window_end_local=window_end_local,
        days_of_week=days_of_week,
        max_concurrency=max_concurrency,
        profile_override=profile_override,
    )
    db.add(batch)
    db.add_all(
        [
            CallBatchTarget(
                batch_id=batch.id,
                target_index=index,
                elder_id=target.elder_id,
                dynamic_vars=target.dynamic_vars,
                profile_override=target.profile_override,
            )
            for index, target in enumerate(targets)
        ]
    )
    await db.flush()
    await db.refresh(batch)
    return batch


async def get_batch(db: AsyncSession, batch_id: uuid.UUID) -> CallBatch | None:
    return await db.get(CallBatch, batch_id)


async def get_by_idempotency_key(db: AsyncSession, key: str) -> CallBatch | None:
    result = await db.execute(select(CallBatch).where(CallBatch.idempotency_key == key))
    return result.scalar_one_or_none()


async def list_batches(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CallBatch]:
    """Most-recent batches, optionally filtered by status.

    Newest first with an ``id`` tiebreaker; ``limit`` clamped to
    1..MAX_BATCHES_LIMIT (spec §8 bounded reads).
    """
    limit = max(1, min(limit, MAX_BATCHES_LIMIT))
    offset = max(0, offset)
    stmt = select(CallBatch)
    if status is not None:
        stmt = stmt.where(CallBatch.status == status)
    stmt = (
        stmt.order_by(CallBatch.created_at.desc(), CallBatch.id.desc()).limit(limit).offset(offset)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_targets(db: AsyncSession, batch_id: uuid.UUID) -> list[CallBatchTarget]:
    result = await db.execute(
        select(CallBatchTarget)
        .where(CallBatchTarget.batch_id == batch_id)
        .order_by(CallBatchTarget.target_index)
    )
    return list(result.scalars().all())


async def list_materialized_targets(db: AsyncSession, batch_id: uuid.UUID) -> list[CallBatchTarget]:
    """The finalizer's working set: unsettled targets with a live call chain."""
    result = await db.execute(
        select(CallBatchTarget)
        .where(CallBatchTarget.batch_id == batch_id, CallBatchTarget.status == "materialized")
        .order_by(CallBatchTarget.target_index)
    )
    return list(result.scalars().all())


async def target_counts(db: AsyncSession, batch_id: uuid.UUID) -> dict[str, int]:
    """Per-status tallies, zero-filled for all five statuses (spec §4.2)."""
    result = await db.execute(
        select(CallBatchTarget.status, func.count())
        .where(CallBatchTarget.batch_id == batch_id)
        .group_by(CallBatchTarget.status)
    )
    counts = dict.fromkeys(TARGET_STATUSES, 0)
    for status, count in result.all():
        counts[status] = count
    return counts


async def final_status_histogram(db: AsyncSession, batch_id: uuid.UUID) -> dict[str, int]:
    """Aggregate of the denormalized ``final_status`` column — never a
    retry-chain walk at read time (spec §3.3)."""
    result = await db.execute(
        select(CallBatchTarget.final_status, func.count())
        .where(CallBatchTarget.batch_id == batch_id, CallBatchTarget.final_status.is_not(None))
        .group_by(CallBatchTarget.final_status)
    )
    return {status: count for status, count in result.all()}


async def trigger_due_batches(db: AsyncSession, *, now: datetime, limit: int) -> list[CallBatch]:
    """Claim due ``scheduled`` batches and flip them ``running`` (spec §5.2 phase 2).

    ``trigger_at IS NULL`` means "next poll cycle", so it is due immediately;
    the predicate matches ``idx_call_batches_due`` exactly. FOR UPDATE SKIP
    LOCKED lets concurrent pollers claim disjoint rows without blocking.
    """
    result = await db.execute(
        select(CallBatch)
        .where(
            CallBatch.status == "scheduled",
            or_(CallBatch.trigger_at.is_(None), CallBatch.trigger_at <= now),
        )
        .order_by(CallBatch.trigger_at.asc().nulls_first(), CallBatch.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    batches = list(result.scalars().all())
    for batch in batches:
        batch.status = "running"
        batch.started_at = now
    await db.flush()
    return batches


async def claim_next_pending_target(db: AsyncSession) -> CallBatchTarget | None:
    """Lock and return the next claimable ``pending`` target, or ``None``.

    Pending targets of ``running`` batches only, ordered ``(batch_id,
    target_index)``; FOR UPDATE OF call_batch_targets SKIP LOCKED so concurrent
    pollers claim disjoint rows (the batch row itself is never locked). The row
    stays ``pending`` and locked until the caller's transaction ends — the
    caller materializes/skips it and commits one row per transaction (§5.2).

    A batch whose materialized-target count has reached ``max_concurrency`` is
    passed over without starving other batches (``NULL`` = unthrottled).

    NOTE (deliberate, benign deviation from spec §5.2 wording): the spec
    throttles on "unsettled targets' in-flight chains" (via
    idx_call_batch_targets_call); counting status='materialized' is equivalent
    ONLY because phase 1 (finalizer) runs before phase 4 every cycle and
    settles drained chains to done/skipped. If the phase order ever changes,
    revisit this throttle.
    """
    other = aliased(CallBatchTarget)
    materialized_count = (
        select(func.count())
        .select_from(other)
        .where(other.batch_id == CallBatchTarget.batch_id, other.status == "materialized")
        .correlate(CallBatchTarget)
        .scalar_subquery()
    )
    result = await db.execute(
        select(CallBatchTarget)
        .join(CallBatch, CallBatch.id == CallBatchTarget.batch_id)
        .where(
            CallBatch.status == "running",
            CallBatchTarget.status == "pending",
            or_(
                CallBatch.max_concurrency.is_(None),
                materialized_count < CallBatch.max_concurrency,
            ),
        )
        .order_by(CallBatchTarget.batch_id, CallBatchTarget.target_index)
        .limit(1)
        .with_for_update(of=CallBatchTarget, skip_locked=True)
    )
    return result.scalars().first()


async def mark_target_materialized(
    db: AsyncSession, target: CallBatchTarget, *, call_id: uuid.UUID, now: datetime
) -> bool:
    """``pending → materialized`` + root ``call_id`` link; guarded no-op otherwise."""
    if target.status != "pending":
        return False
    target.status = "materialized"
    target.call_id = call_id
    target.materialized_at = now
    await db.flush()
    return True


async def mark_target_skipped(
    db: AsyncSession, target: CallBatchTarget, *, reason: str, now: datetime
) -> bool:
    """``pending → skipped`` with a bounded ``skip_reason``; guarded no-op otherwise."""
    if target.status != "pending":
        return False
    target.status = "skipped"
    target.skip_reason = reason
    target.finalized_at = now
    await db.flush()
    return True


async def finalize_target(
    db: AsyncSession, target: CallBatchTarget, *, final_status: str, now: datetime
) -> bool:
    """``materialized → done`` + denormalized chain-terminal ``final_status``
    (spec §6.2); guarded no-op otherwise."""
    if target.status != "materialized":
        return False
    target.status = "done"
    target.final_status = final_status
    target.finalized_at = now
    await db.flush()
    return True


async def cancel_batch(db: AsyncSession, batch: CallBatch, *, now: datetime) -> list[uuid.UUID]:
    """Cancel a batch: ``cancelled`` + ``cancelled_at``, all ``pending`` targets
    flipped ``cancelled`` in the same transaction (spec §5.6).

    Guards: ``completed`` raises ValueError (router maps to 409); an already
    ``cancelled`` batch is an idempotent no-op returning ``[]``. Returns the
    materialized targets' root call ids for the caller's guarded chain-tip
    cancel; materialized targets themselves stay put for the finalizer.
    """
    if batch.status == "completed":
        raise ValueError("batch is already completed and cannot be cancelled")
    if batch.status == "cancelled":
        return []
    batch.status = "cancelled"
    batch.cancelled_at = now
    await db.execute(
        update(CallBatchTarget)
        .where(CallBatchTarget.batch_id == batch.id, CallBatchTarget.status == "pending")
        .values(status="cancelled", finalized_at=now)
    )
    result = await db.execute(
        select(CallBatchTarget.call_id)
        .where(
            CallBatchTarget.batch_id == batch.id,
            CallBatchTarget.status == "materialized",
            CallBatchTarget.call_id.is_not(None),
        )
        .order_by(CallBatchTarget.target_index)
    )
    root_call_ids = [call_id for call_id in result.scalars().all() if call_id is not None]
    await db.flush()
    return root_call_ids


async def open_batches(db: AsyncSession, *, limit: int) -> list[CallBatch]:
    """The poller's open working set — ``idx_call_batches_open`` predicate
    verbatim: running batches plus cancelled batches not yet stamped
    ``completed_at`` (the exit condition keeps this set bounded)."""
    result = await db.execute(
        select(CallBatch)
        .where(CallBatch.status.in_(OPEN_BATCH_STATUSES), CallBatch.completed_at.is_(None))
        .order_by(CallBatch.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def complete_drained_batches(db: AsyncSession, *, now: datetime) -> list[CallBatch]:
    """Settle drained batches (spec §5.2 phase 6): ``running`` with zero open
    targets → ``completed`` + ``completed_at``; ``cancelled`` with zero open
    targets → stamp ``completed_at`` only (status stays ``cancelled``), which
    removes it from ``idx_call_batches_open`` permanently."""
    open_target_exists = (
        select(CallBatchTarget.id)
        .where(
            CallBatchTarget.batch_id == CallBatch.id,
            CallBatchTarget.status.in_(OPEN_TARGET_STATUSES),
        )
        .exists()
    )
    result = await db.execute(
        select(CallBatch)
        .where(
            CallBatch.status.in_(OPEN_BATCH_STATUSES),
            CallBatch.completed_at.is_(None),
            ~open_target_exists,
        )
        .order_by(CallBatch.created_at)
        .with_for_update(skip_locked=True)
    )
    drained = list(result.scalars().all())
    for batch in drained:
        if batch.status == "running":
            batch.status = "completed"
        batch.completed_at = now
    await db.flush()
    return drained
