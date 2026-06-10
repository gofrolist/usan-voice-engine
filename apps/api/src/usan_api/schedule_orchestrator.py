"""Scheduler orchestrator — the third in-process poller (spec §5.1-§5.3).

This module holds the shared single-Call materializer (spec §5.3) used by the
poll cycle's schedule phase (3) and batch-target phase (4), plus the poll loop
itself: ``poll_once`` runs the six-phase cycle of spec §5.2 — finalize settled
batch targets, trigger due batches, materialize due schedules, materialize
batch targets under the slot budget, sweep cancelled batches' queued chain
tips, stamp drained batches — and ``run_poller`` is byte-for-byte the retry
orchestrator's loop discipline, wired in main.py lifespan as the third poller
after the retry and retention pollers.

Correctness rests on the spec §2.2 invariants, not on in-process state:
``scheduled_at IS NOT NULL`` marks a poller-owned row (retry child or
schedule/batch root — the existing claim/reclaim predicates already do the
right thing for both), and the deterministic ``sched:``/``batch:``
idempotency keys are the cross-replica/crash guard — a re-poll after a
partial crash, or a second replica, hits the unique key and takes the
verified-ownership replay path instead of dialing twice. Like the retry
orchestrator's count-then-claim gate and the outbound-trunk provisioning
cache in livekit_dispatch, anything racier than the key assumes the
documented single-replica deployment.

Dialing is NOT done here: materialized rows are QUEUED with ``scheduled_at``
set, and the existing retry poller claims and dials them when due —
inheriting the dial-time DNC and quiet-hours re-checks (spec §2.3).
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import quiet_hours, schedule_windows
from usan_api.db.base import CallStatus
from usan_api.db.models import Call, CallBatch, CallBatchTarget, CallSchedule, Elder
from usan_api.db.session import get_session_factory
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.settings import Settings

# Bounded per-cycle working set for the batch trigger (house pattern: 500-cap reads).
_TRIGGER_BATCHES_LIMIT = 500
# Bounded open-batch working set for the finalizer/sweep phases (same house pattern;
# the phase-6 completed_at stamp is the exit condition that keeps the set small).
_OPEN_BATCHES_LIMIT = 500
# Invalid-timezone fail-closed retry cadence: observable, never hot-loops (spec §5.2).
_INVALID_TZ_RETRY = timedelta(hours=1)
# §6.2 chain-settled test: a chain whose tip is in any of these is still live.
_UNSETTLED_TIP_STATUSES = frozenset(
    {CallStatus.QUEUED, CallStatus.DIALING, CallStatus.RINGING, CallStatus.IN_PROGRESS}
)
# Batch dial windows with a NULL days_of_week apply every day (mirrors the
# call_schedules server default of 127).
_EVERY_DAY_MASK = schedule_windows.days_to_mask(schedule_windows.DAY_NAMES)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MaterializeOutcome:
    """Outcome of one materialization attempt (spec §5.3) — the caller maps
    ``result`` onto its bookkeeping (schedule ``last_result`` / target skip)."""

    result: str  # created | replayed | dnc_blocked | skipped_daily_cap | key_conflict
    call: Call | None


async def _replay_or_conflict(
    db: AsyncSession, elder: Elder, idempotency_key: str
) -> MaterializeOutcome:
    """Verified replay after a unique-key IntegrityError (spec §5.3 step 5).

    Adopt the existing row only when it is OURS: same elder and a chain root
    (``parent_call_id IS NULL``). Anything else is a squatted or foreign key —
    ERROR and refuse; never silently link a foreign call. Either way a key
    never dials twice.
    """
    existing = await calls_repo.get_by_idempotency_key(db, idempotency_key)
    if existing is not None and existing.elder_id == elder.id and existing.parent_call_id is None:
        return MaterializeOutcome("replayed", existing)
    logger.bind(elder_id=str(elder.id)).error(
        "Materialization key conflict: existing row is not ours; refusing to adopt"
    )
    return MaterializeOutcome("key_conflict", None)


async def materialize_call(
    db: AsyncSession,
    settings: Settings,
    *,
    elder: Elder,
    idempotency_key: str,
    scheduled_at: datetime,
    local_day: date,
    dynamic_vars: dict[str, Any],
    profile_override: uuid.UUID | None,
) -> MaterializeOutcome:
    """Materialize one autonomous root Call (spec §5.3, shared by phases 3 and 4).

    One Call per transaction — this function only flushes; the call insert and
    the caller's bookkeeping (schedule advance / target flip) commit atomically
    in the caller. Order: daily cap -> advisory phone lock -> DNC -> create; on
    IntegrityError (unique idempotency_key) SAVEPOINT-rollback (begin_nested),
    re-fetch and VERIFY OWNERSHIP (same elder, parent_call_id IS NULL) ->
    replayed, else key_conflict (ERROR log; never silently adopt a foreign row).
    """
    day_start, day_end = schedule_windows.day_bounds_utc(local_day, elder.timezone)
    roots = await calls_repo.count_autonomous_roots(
        db, elder_id=elder.id, day_start=day_start, day_end=day_end
    )
    if roots >= settings.max_autonomous_calls_per_elder_per_day:
        return MaterializeOutcome("skipped_daily_cap", None)

    # The same advisory lock the enqueue gate takes (one lock held at a time,
    # spec §5.2): serializes against concurrent add_dnc/enqueues for this number.
    await dnc_repo.lock_phone(db, elder.phone_e164)
    if await dnc_repo.is_blocked(db, elder.phone_e164):
        # Terminal DNC_BLOCKED row consuming the key — identical to enqueue_call's
        # gate; begin_nested so a key race here also takes the verified replay path.
        try:
            async with db.begin_nested():
                call = await calls_repo.create_materialized_root(
                    db,
                    elder_id=elder.id,
                    status=CallStatus.DNC_BLOCKED,
                    idempotency_key=idempotency_key,
                    scheduled_at=None,
                    dynamic_vars=dynamic_vars,
                    profile_override=profile_override,
                )
        except IntegrityError:
            return await _replay_or_conflict(db, elder, idempotency_key)
        return MaterializeOutcome("dnc_blocked", call)

    try:
        async with db.begin_nested():  # SAVEPOINT: a duplicate key rolls back here only
            call = await calls_repo.create_materialized_root(
                db,
                elder_id=elder.id,
                status=CallStatus.QUEUED,
                idempotency_key=idempotency_key,
                scheduled_at=scheduled_at,
                dynamic_vars=dynamic_vars,
                profile_override=profile_override,
            )
    except IntegrityError:
        return await _replay_or_conflict(db, elder, idempotency_key)
    return MaterializeOutcome("created", call)


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One six-phase poll cycle (spec §5.2); each phase commits before the next.

    Phases 3-4 process ONE row per transaction (claim 1, materialize, commit,
    repeat up to the phase budget) so exactly one per-phone advisory lock is
    held at a time — a time-sensitive ``POST /v1/dnc`` opt-out never queues
    behind a batch-sized lock pile — and crash granularity is a single row.

    Returns per-phase work counts. ``now`` overrides the clock for
    deterministic tests; in production every phase shares one real-time instant.
    """
    moment = now if now is not None else _utcnow()
    counts: dict[str, int] = {}
    counts["targets_finalized"] = await _finalize_settled_targets(factory, now=moment)
    counts["batches_triggered"] = await _trigger_due_batches(factory, now=moment)
    counts["schedules"] = await _materialize_due_schedules(factory, settings, now=moment)
    counts["batch_targets"] = await _materialize_batch_targets(factory, settings, now=moment)
    counts["chains_swept"] = await _sweep_cancelled_batches(factory, now=moment)
    counts["batches_completed"] = await _complete_drained_batches(factory, now=moment)
    return counts


async def _finalize_settled_targets(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Phase 1 — settle finished batch-target chains (spec §5.2, §6.2).

    Chain-settled ⇔ the chain tip's status is terminal (∉ queued/dialing/
    ringing/in_progress) AND the tip has no child row — sound because every
    terminal transition and its ``schedule_retry`` share one commit (§6.2):
    after that commit the child either exists or never will. The denormalized
    ``final_status`` is the tip's status verbatim (completed / no_answer /
    voicemail_left / busy / failed / dnc_blocked / cancelled). One commit per
    open batch keeps transactions short.
    """
    async with factory() as db:
        batch_ids = [b.id for b in await batches_repo.open_batches(db, limit=_OPEN_BATCHES_LIMIT)]
    finalized = 0
    for batch_id in batch_ids:
        async with factory() as db:
            for target in await batches_repo.list_materialized_targets(db, batch_id):
                if target.call_id is None:  # defensive: materialized implies a linked root
                    continue
                tip = await calls_repo.get_chain_tip(db, target.call_id)
                if tip is None or tip.status in _UNSETTLED_TIP_STATUSES:
                    continue
                if await calls_repo.has_child(db, tip.id):
                    continue
                if await batches_repo.finalize_target(
                    db, target, final_status=tip.status.value, now=now
                ):
                    finalized += 1
                    logger.bind(
                        component="schedule_poller",
                        batch_id=str(batch_id),
                        target_index=target.target_index,
                        final_status=tip.status.value,
                    ).info("Batch target finalized")
            await db.commit()
    return finalized


async def _trigger_due_batches(factory: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
    """Phase 2 — flip due ``scheduled`` batches to ``running`` (spec §5.2).

    ``trigger_at IS NULL`` means "next poll cycle", so it is due immediately.
    """
    async with factory() as db:
        batches = await batches_repo.trigger_due_batches(db, now=now, limit=_TRIGGER_BATCHES_LIMIT)
        batch_ids = [batch.id for batch in batches]
        await db.commit()
    for batch_id in batch_ids:
        logger.bind(component="schedule_poller", batch_id=str(batch_id)).info(
            "Batch triggered: scheduled -> running"
        )
    return len(batch_ids)


async def _materialize_due_schedules(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime
) -> int:
    """Phase 3 — materialize due schedules, ONE row per transaction (spec §5.2).

    Deliberately unthrottled: the daily wellness call outranks campaign traffic
    (phase 4 is the slot-budgeted one); ``scheduler_batch_size`` only bounds the
    per-cycle claim count so a cycle stays finite.
    """
    processed = 0
    for _ in range(settings.scheduler_batch_size):
        async with factory() as db:
            claimed = await schedules_repo.claim_due_schedules(db, now=now, limit=1)
            if not claimed:
                break
            await _materialize_one_schedule(db, settings, claimed[0], now=now)
            await db.commit()
        processed += 1
    return processed


async def _materialize_one_schedule(
    db: AsyncSession, settings: Settings, schedule: CallSchedule, *, now: datetime
) -> None:
    """Run the exhaustive §5.2 phase-3 branch matrix for one claimed schedule.

    EVERY branch writes ``last_result``/``last_result_at`` AND advances
    ``next_run_at`` — a branch that forgets the advance re-claims the same row
    every cycle forever (§5.3 step 5). The call insert and this bookkeeping
    commit atomically in the caller.
    """
    log = logger.bind(component="schedule_poller", schedule_id=str(schedule.id))
    elder = await db.get(Elder, schedule.elder_id)
    try:
        if elder is None:
            # CASCADE FK makes this unreachable while the claim lock is held;
            # fail closed onto the hourly-retry branch rather than crash the cycle.
            raise ValueError("schedule elder row missing")
        window = schedule_windows.effective_window(
            schedule.window_start_local, schedule.window_end_local
        )
        if window is None:  # validated non-empty at create; fail closed regardless
            raise ValueError("schedule window never intersects quiet hours")
        today = schedule_windows.local_date(now, elder.timezone)
        start_utc, end_utc = schedule_windows.window_bounds_utc(
            today, elder.timezone, window_start=window[0], window_end=window[1]
        )

        if not schedule.days_of_week & (1 << today.weekday()) or now < start_utc:
            # Stale next_run_at (tz/window edit): recompute under the CURRENT
            # timezone — no call, no last_materialized_date, no skipped day.
            await schedules_repo.record_result(
                db,
                schedule,
                result="rescheduled",
                now=now,
                next_run_at=schedule_windows.next_run_at(
                    now,
                    elder.timezone,
                    window_start=schedule.window_start_local,
                    window_end=schedule.window_end_local,
                    days_mask=schedule.days_of_week,
                ),
            )
            log.info("Schedule rescheduled: next_run_at was stale for the current window")
            return

        next_occurrence = schedule_windows.next_run_at(
            end_utc,
            elder.timezone,
            window_start=schedule.window_start_local,
            window_end=schedule.window_end_local,
            days_mask=schedule.days_of_week,
        )

        if now >= end_utc:
            # Poller-outage semantics: skip observably, never a late call (§5.5).
            await schedules_repo.record_result(
                db, schedule, result="skipped_window", now=now, next_run_at=next_occurrence
            )
            log.warning("Schedule window already ended; occurrence skipped observably")
            return

        outcome = await materialize_call(
            db,
            settings,
            elder=elder,
            idempotency_key=f"sched:{schedule.id}:{today.isoformat()}",
            scheduled_at=quiet_hours.next_allowed(now, elder.timezone),
            local_day=today,
            dynamic_vars=schedule.dynamic_vars,
            profile_override=schedule.profile_override,
        )
    except ValueError:
        # Invalid timezone (or equivalent corrupt state): fail CLOSED — no call,
        # observable result, hourly retry so it never hot-loops (§5.2).
        await schedules_repo.record_result(
            db,
            schedule,
            result="skipped_invalid_timezone",
            now=now,
            next_run_at=now + _INVALID_TZ_RETRY,
        )
        log.opt(exception=True).error(
            "Schedule materialization failed closed (invalid timezone); retrying hourly"
        )
        return

    await _record_materialize_outcome(
        db, schedule, outcome, now=now, next_occurrence=next_occurrence, today=today, log=log
    )


async def _record_materialize_outcome(
    db: AsyncSession,
    schedule: CallSchedule,
    outcome: MaterializeOutcome,
    *,
    now: datetime,
    next_occurrence: datetime,
    today: date,
    log: Any,
) -> None:
    """Map a ``materialize_call`` outcome onto schedule bookkeeping (spec §5.3)."""
    if outcome.result in ("created", "replayed"):
        await schedules_repo.record_result(
            db,
            schedule,
            result=outcome.result,
            now=now,
            next_run_at=next_occurrence,
            last_materialized_date=today,
        )
        call_id = str(outcome.call.id) if outcome.call is not None else None
        log.bind(call_id=call_id).info("Schedule materialized: {r}", r=outcome.result)
    elif outcome.result == "dnc_blocked":
        # Auto-disable: a daily schedule must not mint one DNC_BLOCKED row per
        # day forever; the operator re-enables after DNC removal (§5.3 step 3).
        await schedules_repo.record_result(
            db,
            schedule,
            result="dnc_blocked",
            now=now,
            next_run_at=next_occurrence,
            enabled=False,
        )
        log.warning("Schedule auto-disabled: elder number is on the DNC list")
    elif outcome.result == "key_conflict":
        await schedules_repo.record_result(
            db, schedule, result="key_conflict", now=now, next_run_at=next_occurrence
        )
        log.error("Schedule idempotency-key conflict; occurrence advanced, no call linked")
    else:  # skipped_daily_cap
        await schedules_repo.record_result(
            db, schedule, result=outcome.result, now=now, next_run_at=next_occurrence
        )
        log.info("Schedule skipped: {r}", r=outcome.result)


async def _materialize_batch_targets(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime
) -> int:
    """Phase 4 — slot-budgeted batch-target materialization (spec §5.2).

    The slot budget — ``max_concurrent_calls − reserved_concurrency − in_flight
    − queued_due`` — is INTRINSIC: it applies regardless of
    ``CONCURRENCY_GATE_ENABLED``, which governs only the retry poller's *claim*
    path. The budget is soft pacing that keeps the due queue shallow so batch
    progress reporting reflects reality; the claim gate remains the hard cap.
    Per-batch ``max_concurrency`` (enforced in ``claim_next_pending_target``)
    and this budget are materialization throttles, NOT dial caps — retry
    children of already-materialized chains dial whenever due, bounded only by
    the global gate. Phase 3 runs first and unthrottled: the daily wellness
    call outranks campaign traffic, so a due schedule cohort consumes slots
    before any batch target is considered (deliberate priority).
    """
    async with factory() as db:  # one txn: count + budget share a snapshot
        in_flight = await calls_repo.count_in_flight(
            db, now=now, max_age_s=settings.outbound_max_call_duration_s + 120
        )
        queued_due = await calls_repo.count_queued_due(db, now=now)
    slots = max(
        0, settings.max_concurrent_calls - settings.reserved_concurrency - in_flight - queued_due
    )
    processed = 0
    for _ in range(min(settings.scheduler_batch_size, slots)):
        async with factory() as db:  # ONE target per transaction (spec §5.2)
            target = await batches_repo.claim_next_pending_target(db)
            if target is None:
                break
            await _materialize_one_target(db, settings, target, now=now)
            await db.commit()
        processed += 1
    return processed


async def _materialize_one_target(
    db: AsyncSession, settings: Settings, target: CallBatchTarget, *, now: datetime
) -> None:
    """Run the spec §5.2 phase-4 branch matrix for one claimed pending target.

    Every branch settles or links the claimed row in the caller's transaction
    (call insert + target flip commit atomically, §5.3) — a claimed target is
    never left pending with work done.
    """
    log = logger.bind(
        component="schedule_poller",
        batch_id=str(target.batch_id),
        target_index=target.target_index,
    )
    elder = await db.get(Elder, target.elder_id) if target.elder_id is not None else None
    if elder is None:
        # FK SET NULL orphaned the target (spec §3.3): a deleted elder must not
        # silently shrink the batch — skip observably.
        await batches_repo.mark_target_skipped(db, target, reason="elder_deleted", now=now)
        log.info("Batch target skipped: elder deleted")
        return
    batch = await db.get(CallBatch, target.batch_id)
    try:
        dial_at = quiet_hours.next_allowed(now, elder.timezone)
        if (
            batch is not None
            and batch.window_start_local is not None
            and batch.window_end_local is not None
        ):
            # Push the clamp into the batch window/day-mask (first attempts only,
            # §6.1; next_run_at re-intersects with quiet hours).
            dial_at = schedule_windows.next_run_at(
                dial_at,
                elder.timezone,
                window_start=batch.window_start_local,
                window_end=batch.window_end_local,
                days_mask=batch.days_of_week if batch.days_of_week is not None else _EVERY_DAY_MASK,
            )
        # The PUSHED dial day: the daily cap counts the day the call will actually
        # happen, never `now`'s day — a window-pushed target must not dodge the
        # harassment cap (spec §5.3 step 1).
        local_day = schedule_windows.local_date(dial_at, elder.timezone)
    except ValueError:
        # Invalid timezone (or a window that can no longer fire): fail CLOSED —
        # never risk a TCPA-hour dial on corrupt state.
        await batches_repo.mark_target_skipped(db, target, reason="invalid_timezone", now=now)
        log.opt(exception=True).error("Batch target failed closed: invalid timezone")
        return
    profile_override = target.profile_override  # target override wins over batch default (§5.3)
    if profile_override is None and batch is not None:
        profile_override = batch.profile_override
    outcome = await materialize_call(
        db,
        settings,
        elder=elder,
        idempotency_key=f"batch:{target.batch_id}:{target.target_index}",
        scheduled_at=dial_at,
        local_day=local_day,
        dynamic_vars=target.dynamic_vars,
        profile_override=profile_override,
    )
    if outcome.result in ("created", "replayed", "dnc_blocked") and outcome.call is not None:
        await batches_repo.mark_target_materialized(db, target, call_id=outcome.call.id, now=now)
        bound = log.bind(call_id=str(outcome.call.id))
        if outcome.result == "dnc_blocked":
            # Terminal row consumed the key; the finalizer settles the target
            # done/dnc_blocked next cycle (§5.3 step 3) — never silent.
            bound.warning("Batch target materialized as DNC_BLOCKED")
        else:
            bound.info("Batch target materialized: {r}", r=outcome.result)
    elif outcome.result == "skipped_daily_cap":
        await batches_repo.mark_target_skipped(db, target, reason="daily_cap", now=now)
        log.info("Batch target skipped: daily autonomous-call cap reached on the dial day")
    else:  # key_conflict
        await batches_repo.mark_target_skipped(db, target, reason="key_conflict", now=now)
        log.error("Batch target idempotency-key conflict; skipped, no call linked")


async def _sweep_cancelled_batches(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Phase 5 — backstop sweep (spec §5.2, §5.6): guarded-cancel QUEUED chain
    tips of open cancelled batches.

    The PRIMARY guard is the cancellation-aware ``schedule_retry`` (suppresses
    post-cancel children in the parent's terminal commit); this sweep only
    closes the narrow cancel-vs-terminal-transition commit race. In-flight tips
    are never torn down — they finish naturally and the finalizer settles them
    with their truthful outcome. (``now`` is unused: the guarded tip flip needs
    no clock; the parameter keeps the six phase signatures uniform.)
    """
    async with factory() as db:
        cancelled_ids = [
            batch.id
            for batch in await batches_repo.open_batches(db, limit=_OPEN_BATCHES_LIMIT)
            if batch.status == "cancelled"
        ]
    swept = 0
    for batch_id in cancelled_ids:
        async with factory() as db:
            targets = await batches_repo.list_materialized_targets(db, batch_id)
            roots = [t.call_id for t in targets if t.call_id is not None]
            flipped = await calls_repo.cancel_queued_tips(db, roots)
            await db.commit()
        if flipped:
            logger.bind(component="schedule_poller", batch_id=str(batch_id)).info(
                "Sweep cancelled {n} queued chain tip(s)", n=flipped
            )
        swept += flipped
    return swept


async def _complete_drained_batches(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Phase 6 — stamp drained batches (spec §5.2): ``running`` with zero open
    targets → ``completed`` + ``completed_at``; ``cancelled`` with zero open
    targets → ``completed_at`` only (status preserved). The stamp removes the
    batch from the open working set permanently — phases 1/5 never revisit
    drained history (§9 cancelled-batch drain bookkeeping)."""
    async with factory() as db:
        drained = await batches_repo.complete_drained_batches(db, now=now)
        stamped = [(batch.id, batch.status) for batch in drained]
        await db.commit()
    for batch_id, status in stamped:
        logger.bind(component="schedule_poller", batch_id=str(batch_id), status=status).info(
            "Batch drained: completed_at stamped"
        )
    return len(stamped)


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop ``poll_once`` on the configured interval until ``stop`` is set.

    Byte-for-byte the retry orchestrator's loop discipline: per-cycle exceptions
    are logged, never fatal; the interval sleep is a cancellable wait on
    ``stop``, so shutdown is prompt.
    """
    log = logger.bind(component="schedule_poller")
    log.info("Schedule poller started (interval={i}s)", i=settings.scheduler_poll_interval_s)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            counts = await poll_once(factory, settings)
            if any(counts.values()):
                log.info("Scheduler cycle work: {counts}", counts=counts)
        except Exception:
            log.opt(exception=True).error("Schedule poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_poll_interval_s)
    log.info("Schedule poller stopped")
