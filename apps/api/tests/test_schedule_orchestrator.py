"""Scheduler poller — all six poll phases + loop discipline.

Pins the exhaustive spec §5.2 phase-3 branch matrix — created / replayed /
rescheduled / skipped_window / skipped_invalid_timezone / dnc_blocked /
key_conflict — where EVERY branch writes ``last_result`` AND advances
``next_run_at`` (omitting the advance on any branch is the §5.3(5)/§9 infinite
re-claim loop), the phase-2 batch trigger, open-transaction SKIP LOCKED
disjointness, and the run_poller loop discipline cloned from the retry poller.

Plus the batch-target phases (spec §5.2 phases 1/4/5/6): the intrinsic
flag-independent slot budget, schedule-over-batch priority, the per-target skip
branches (elder_deleted / invalid_timezone / daily_cap / key_conflict), the
window-pushed dial time capped against the PUSHED day, cross-process crash
resume via verified key replay, the §6.2 finalizer matrix, the cancelled-batch
sweep backstop, and drained-batch completion bookkeeping.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from loguru import logger
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import counter_value
from usan_api import quiet_hours, schedule_orchestrator, schedule_windows
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch, CallBatchTarget, CallSchedule, Elder
from usan_api.observability.custom_metrics import MATERIALIZED_CALLS_TOTAL
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn
from usan_api.settings import Settings

# Wednesday 2026-06-10 15:00Z = 11:00 EDT — inside the default 09:00-17:00 window.
NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
TODAY = date(2026, 6, 10)
# Next masked day's effective-window start for a 127-mask 09:00-17:00 NY window:
# Thursday 2026-06-11 09:00 EDT = 13:00Z.
NEXT_DAY_START = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE call_batch_targets, call_batches, call_schedules, calls, "
                "dnc_list, elders CASCADE"
            )
        )
        await db.commit()


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


# Sentinel: "no profile at all" — distinct from None, which publishes a profile
# WITHOUT a `policy` section (resolves, statutory by whole-profile precedence).
_NO_PROFILE = object()


async def _publish_policy_profile(db, *, policy=None):
    """Create a profile, optionally set a `policy` section, publish it; returns the id."""
    profile = await profiles_repo.create_profile(
        db, name=f"policy-{uuid.uuid4().hex}", description=None, actor_email="t@usan.test"
    )
    if policy is not None:
        cfg = dict(profile.draft_config)
        cfg["policy"] = policy
        await profiles_repo.update_draft(
            db, profile.id, config=cfg, description=None, actor_email="t@usan.test"
        )
    await profiles_repo.publish(db, profile.id, note=None, actor_email="t@usan.test")
    return profile.id


async def _seed_elder(factory, *, timezone: str = "America/New_York", policy=_NO_PROFILE) -> Elder:
    """One elder; ``policy``: ``_NO_PROFILE`` leaves them profile-less, ``None``
    assigns a published profile with no ``policy`` section, a dict publishes
    that policy and assigns it (the test_dispatch_and_dial.py discipline)."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="S", phone_e164=phone, timezone=timezone)
        if policy is not _NO_PROFILE:
            elder.agent_profile_id = await _publish_policy_profile(db, policy=policy)
        await db.commit()
    return elder


async def _seed_schedule(
    factory,
    elder_id: uuid.UUID,
    *,
    next_run_at: datetime,
    window_start: time = time(9, 0),
    window_end: time = time(17, 0),
    days_of_week: int = 127,
    profile_override: uuid.UUID | None = None,
) -> uuid.UUID:
    async with factory() as db:
        row = await schedules_repo.create_schedule(
            db,
            elder_id=elder_id,
            window_start_local=window_start,
            window_end_local=window_end,
            days_of_week=days_of_week,
            enabled=True,
            dynamic_vars={},
            profile_override=profile_override,
            next_run_at=next_run_at,
        )
        await db.commit()
        return row.id


async def _seed_batch(factory, *, trigger_at: datetime | None) -> uuid.UUID:
    elder = await _seed_elder(factory)
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="campaign",
            idempotency_key=None,
            payload_digest="d" * 64,
            trigger_at=trigger_at,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=None,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=elder.id)],
        )
        await db.commit()
        return batch.id


async def _seed_call_with_key(factory, *, elder_id: uuid.UUID, key: str) -> uuid.UUID:
    """Pre-existing root carrying an idempotency key (replay/conflict scenarios)."""
    async with factory() as db:
        call = Call(
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            idempotency_key=key,
            scheduled_at=NOW,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id


async def _get_schedule(factory, schedule_id: uuid.UUID) -> CallSchedule:
    async with factory() as db:
        row = await db.get(CallSchedule, schedule_id)
        assert row is not None
        return row


async def _set_schedule(factory, schedule_id: uuid.UUID, **values) -> None:
    async with factory() as db:
        await db.execute(
            update(CallSchedule).where(CallSchedule.id == schedule_id).values(**values)
        )
        await db.commit()


async def _set_elder(factory, elder_id: uuid.UUID, **values) -> None:
    async with factory() as db:
        await db.execute(update(Elder).where(Elder.id == elder_id).values(**values))
        await db.commit()


async def _calls(factory) -> list[Call]:
    async with factory() as db:
        result = await db.execute(select(Call).order_by(Call.created_at, Call.id))
        return list(result.scalars().all())


async def test_poll_once_materializes_due_schedule_inside_window(session_factory):
    elder = await _seed_elder(session_factory)  # NY: NOW is 11:00 EDT, inside 09:00-17:00
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=1))

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    call = calls[0]
    assert call.status is CallStatus.QUEUED
    assert call.elder_id == elder.id
    assert call.idempotency_key == f"sched:{sid}:{TODAY.isoformat()}"
    assert call.scheduled_at == quiet_hours.next_allowed(NOW, "America/New_York")
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "created"
    assert schedule.last_materialized_date == TODAY
    assert schedule.next_run_at == NEXT_DAY_START  # advanced to the next masked day's start


async def test_poll_once_twice_is_idempotent_with_full_bookkeeping(session_factory):
    elder = await _seed_elder(session_factory)
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=1))
    await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    # Simulate the crash-before-bookkeeping window: the call row committed but
    # next_run_at was rewound to due — the §5.3(5)/§9 replay regression.
    await _set_schedule(session_factory, sid, next_run_at=NOW - timedelta(hours=1))

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1  # exactly one call — the key never dials twice
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "replayed"
    assert schedule.last_materialized_date == TODAY
    # Omitting this advance would re-claim and "replay" the same row every cycle forever.
    assert schedule.next_run_at == NEXT_DAY_START


async def test_schedule_key_conflict_records_and_advances(session_factory):
    owner = await _seed_elder(session_factory)
    foreign = await _seed_elder(session_factory)
    sid = await _seed_schedule(session_factory, owner.id, next_run_at=NOW - timedelta(hours=1))
    key = f"sched:{sid}:{TODAY.isoformat()}"
    foreign_call_id = await _seed_call_with_key(session_factory, elder_id=foreign.id, key=key)

    errors: list[str] = []
    handler_id = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    calls = await _calls(session_factory)
    assert len(calls) == 1  # no adoption: no second row minted
    assert calls[0].id == foreign_call_id
    assert calls[0].elder_id == foreign.id  # the foreign row untouched
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "key_conflict"
    # A missing advance here is the same infinite re-claim loop class as replay.
    assert schedule.next_run_at == NEXT_DAY_START
    assert errors


async def test_past_window_end_skips_observably(session_factory):
    elder = await _seed_elder(session_factory)  # NOW = 11:00 EDT, past a 09:00-10:30 window
    sid = await _seed_schedule(
        session_factory,
        elder.id,
        next_run_at=NOW - timedelta(hours=2),
        window_start=time(9, 0),
        window_end=time(10, 30),
    )

    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    assert counts["schedules"] == 1
    assert await _calls(session_factory) == []  # never a 23:00 call (§5.2/§5.5)
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "skipped_window"
    assert schedule.last_materialized_date is None
    # Next occurrence: Thursday 09:00 EDT = 13:00Z.
    assert schedule.next_run_at == NEXT_DAY_START
    assert warnings


async def test_before_window_start_reschedules_without_skipping_day(session_factory):
    elder = await _seed_elder(session_factory)  # created NY (09:00 EDT = 13:00Z)
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=2))
    # Westward tz edit AFTER create: 09:00 local is now 16:00Z, so NOW (15:00Z)
    # is before today's window start under the CURRENT timezone.
    await _set_elder(session_factory, elder.id, timezone="America/Los_Angeles")

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 1
    assert await _calls(session_factory) == []
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "rescheduled"
    assert schedule.last_materialized_date is None  # the day is NOT skipped
    # Recomputed under the current tz: TODAY 09:00 PDT = 16:00Z.
    assert schedule.next_run_at == datetime(2026, 6, 10, 16, 0, tzinfo=UTC)


async def test_invalid_timezone_fails_closed_hourly(session_factory):
    elder = await _seed_elder(session_factory)
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=1))
    await _set_elder(session_factory, elder.id, timezone="Not/AZone")

    errors: list[str] = []
    handler_id = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    assert counts["schedules"] == 1
    assert await _calls(session_factory) == []  # fail closed: never a dial on a bad zone
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "skipped_invalid_timezone"
    assert schedule.next_run_at == NOW + timedelta(hours=1)  # observable, never hot-loops
    assert errors


async def test_dnc_auto_disables_schedule(session_factory):
    elder = await _seed_elder(session_factory)
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=1))
    async with session_factory() as db:
        await dnc_repo.add_entry(db, elder.phone_e164, "asked to stop")
        await db.commit()

    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    assert calls[0].status is CallStatus.DNC_BLOCKED  # terminal row consuming the key
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.enabled is False  # auto-disabled (§5.3 step 3)
    assert schedule.last_result == "dnc_blocked"
    assert warnings

    # A daily schedule must not mint one DNC_BLOCKED row per day forever: even
    # rewound to due, the disabled schedule is never claimed again.
    await _set_schedule(session_factory, sid, next_run_at=NOW - timedelta(hours=1))
    counts2 = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    assert counts2["schedules"] == 0
    assert len(await _calls(session_factory)) == 1


async def test_trigger_due_batches_phase(session_factory):
    due_past = await _seed_batch(session_factory, trigger_at=NOW - timedelta(minutes=5))
    due_null = await _seed_batch(session_factory, trigger_at=None)  # "next poll cycle"
    future = await _seed_batch(session_factory, trigger_at=NOW + timedelta(hours=1))

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["batches_triggered"] == 2
    async with session_factory() as db:
        past_row = await db.get(CallBatch, due_past)
        null_row = await db.get(CallBatch, due_null)
        future_row = await db.get(CallBatch, future)
    assert past_row is not None
    assert null_row is not None
    assert future_row is not None
    assert past_row.status == "running"
    assert past_row.started_at == NOW
    assert null_row.status == "running"
    assert null_row.started_at == NOW
    assert future_row.status == "scheduled"
    assert future_row.started_at is None


async def test_concurrent_poll_once_disjoint_claims(session_factory, async_database_url):
    # §9 SKIP LOCKED race, open-transaction interleaving (precedent:
    # test_claim_skips_locked_rows) — sequential runs prove nothing because the
    # first commit advances next_run_at and the second poll trivially finds nothing.
    elder_a = await _seed_elder(session_factory)
    elder_b = await _seed_elder(session_factory)
    sid_a = await _seed_schedule(session_factory, elder_a.id, next_run_at=NOW - timedelta(hours=2))
    sid_b = await _seed_schedule(session_factory, elder_b.id, next_run_at=NOW - timedelta(hours=1))

    engine_a = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
        async with factory_a() as db_a:
            locked = await schedules_repo.claim_due_schedules(db_a, now=NOW, limit=1)
            assert [s.id for s in locked] == [sid_a]  # A holds the row lock (txn open)

            # With the lock held, a full poll via factory B materializes ONLY the
            # unlocked schedule and does not block.
            counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
            assert counts["schedules"] == 1
            calls = await _calls(session_factory)
            assert [c.idempotency_key for c in calls] == [f"sched:{sid_b}:{TODAY.isoformat()}"]

            await db_a.rollback()  # release the lock; next_run_at unchanged
    finally:
        await engine_a.dispose()

    counts2 = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    assert counts2["schedules"] == 1  # the released schedule materializes now

    calls = await _calls(session_factory)
    assert len(calls) == 2
    assert {c.idempotency_key for c in calls} == {
        f"sched:{sid_a}:{TODAY.isoformat()}",
        f"sched:{sid_b}:{TODAY.isoformat()}",
    }  # key uniqueness held throughout


async def test_run_poller_exits_promptly_when_stop_preset(monkeypatch):
    monkeypatch.setattr(schedule_orchestrator, "get_session_factory", lambda: None)
    calls_count = {"n": 0}

    async def _fake_poll(factory, settings, *, now=None):
        calls_count["n"] += 1
        return {}

    monkeypatch.setattr(schedule_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(schedule_orchestrator.run_poller(_settings(), stop), timeout=2)
    assert calls_count["n"] == 0  # stop preset -> never polls, never sleeps the interval


async def test_run_poller_survives_poll_exception(monkeypatch):
    monkeypatch.setattr(schedule_orchestrator, "get_session_factory", lambda: None)
    stop = asyncio.Event()
    n = {"i": 0}

    async def _flaky(factory, settings, *, now=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("transient boom")
        stop.set()
        return {}

    monkeypatch.setattr(schedule_orchestrator, "poll_once", _flaky)

    settings = _settings()
    settings.scheduler_poll_interval_s = 0.01  # Settings has no validate_assignment; spin fast
    await asyncio.wait_for(schedule_orchestrator.run_poller(settings, stop), timeout=2)
    assert n["i"] >= 2  # exception in cycle 1 did not kill the loop


async def test_run_poller_stop_interrupts_interval_sleep(monkeypatch):
    monkeypatch.setattr(schedule_orchestrator, "get_session_factory", lambda: None)

    async def _fake_poll(factory, settings, *, now=None):
        return {}

    monkeypatch.setattr(schedule_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    settings = _settings()  # default 60s interval
    task = asyncio.create_task(schedule_orchestrator.run_poller(settings, stop))
    await asyncio.sleep(0.05)  # let one cycle run and enter the interval sleep
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # must return well before 60s


# --- D9: phases 1/4/5/6 — finalizer, slot-budgeted batch materialization, sweep, completion ---

# Batch-window push target: next Monday after NOW (Wed 2026-06-10) at 10:00 EDT = 14:00Z.
PUSHED_MONDAY_DIAL = datetime(2026, 6, 15, 14, 0, tzinfo=UTC)


async def _seed_call(
    factory,
    *,
    elder_id: uuid.UUID | None = None,
    status: CallStatus = CallStatus.QUEUED,
    scheduled_at: datetime | None = None,
    key: str | None = None,
    parent_call_id: uuid.UUID | None = None,
    attempt: int = 1,
    fresh: bool = False,
) -> uuid.UUID:
    """Insert one call row (own elder unless given); ``fresh`` forces
    ``updated_at == NOW`` so the row passes the in-flight recency bound."""
    if elder_id is None:
        elder_id = (await _seed_elder(factory)).id
    async with factory() as db:
        call = Call(
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=status,
            idempotency_key=key,
            scheduled_at=scheduled_at,
            parent_call_id=parent_call_id,
            attempt=attempt,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        await db.commit()
        call_id = call.id
    if fresh:
        async with factory() as db:
            await db.execute(update(Call).where(Call.id == call_id).values(updated_at=NOW))
            await db.commit()
    return call_id


async def _seed_running_batch(
    factory,
    *,
    elder_ids: list[uuid.UUID],
    status: str = "running",
    window_start: time | None = None,
    window_end: time | None = None,
    days_of_week: int | None = None,
    profile_override: uuid.UUID | None = None,
) -> uuid.UUID:
    """One batch with a pending target per elder, forced straight to ``status``."""
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="campaign",
            idempotency_key=None,
            payload_digest="d" * 64,
            trigger_at=None,
            window_start_local=window_start,
            window_end_local=window_end,
            days_of_week=days_of_week,
            max_concurrency=None,
            profile_override=profile_override,
            targets=[BatchTargetIn(elder_id=eid) for eid in elder_ids],
        )
        batch.status = status
        batch.started_at = NOW
        if status == "cancelled":
            batch.cancelled_at = NOW
        await db.commit()
        return batch.id


async def _targets(factory, batch_id: uuid.UUID) -> list[CallBatchTarget]:
    async with factory() as db:
        return await batches_repo.list_targets(db, batch_id)


async def _set_target(factory, target_id: int, **values) -> None:
    async with factory() as db:
        await db.execute(
            update(CallBatchTarget).where(CallBatchTarget.id == target_id).values(**values)
        )
        await db.commit()


async def _set_call(factory, call_id: uuid.UUID, **values) -> None:
    async with factory() as db:
        await db.execute(update(Call).where(Call.id == call_id).values(**values))
        await db.commit()


async def _get_call(factory, call_id: uuid.UUID) -> Call:
    async with factory() as db:
        call = await db.get(Call, call_id)
        assert call is not None
        return call


async def _get_batch(factory, batch_id: uuid.UUID) -> CallBatch:
    async with factory() as db:
        batch = await db.get(CallBatch, batch_id)
        assert batch is not None
        return batch


async def _delete_elder(factory, elder_id: uuid.UUID) -> None:
    async with factory() as db:
        await db.execute(delete(Elder).where(Elder.id == elder_id))
        await db.commit()


async def _run_slot_budget_case(session_factory, *, gate: str) -> None:
    """Gate math: slots = 5 max − 2 reserved − 1 in-flight − 1 queued-due = 1."""
    await _seed_call(session_factory, status=CallStatus.IN_PROGRESS, fresh=True)
    await _seed_call(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    elder_ids = [(await _seed_elder(session_factory)).id for _ in range(3)]
    batch_id = await _seed_running_batch(session_factory, elder_ids=elder_ids)

    counts = await schedule_orchestrator.poll_once(
        session_factory,
        _settings(
            CONCURRENCY_GATE_ENABLED=gate, MAX_CONCURRENT_CALLS="5", RESERVED_CONCURRENCY="2"
        ),
        now=NOW,
    )

    assert counts["batch_targets"] == 1  # exactly one slot, exactly one target per cycle
    targets = await _targets(session_factory, batch_id)
    assert [t.status for t in targets] == ["materialized", "pending", "pending"]
    assert targets[0].call_id is not None
    call = await _get_call(session_factory, targets[0].call_id)
    assert call.idempotency_key == f"batch:{batch_id}:0"
    assert call.status is CallStatus.QUEUED


async def test_batch_targets_materialize_with_slot_budget(session_factory):
    # CONCURRENCY_GATE_ENABLED pinned ON explicitly (§9 flag matrix).
    await _run_slot_budget_case(session_factory, gate="true")


async def test_batch_budget_applies_even_with_gate_disabled(session_factory):
    # Same seeding, gate OFF: the phase-4 budget is intrinsic and flag-independent
    # (spec §5.2 phase 4) — the gate flag governs only the retry-poller claim path.
    await _run_slot_budget_case(session_factory, gate="false")


async def test_schedules_outrank_batches(session_factory):
    # slots before phase 3: 4 max − 2 reserved − 1 in-flight = 1.
    await _seed_call(session_factory, status=CallStatus.IN_PROGRESS, fresh=True)
    sched_elder = await _seed_elder(session_factory)
    sid = await _seed_schedule(
        session_factory, sched_elder.id, next_run_at=NOW - timedelta(hours=1)
    )
    batch_elder = await _seed_elder(session_factory)
    batch_id = await _seed_running_batch(session_factory, elder_ids=[batch_elder.id])
    settings = _settings(MAX_CONCURRENT_CALLS="4", RESERVED_CONCURRENCY="2")

    counts = await schedule_orchestrator.poll_once(session_factory, settings, now=NOW)

    # Phase 3 (unthrottled, runs first) took the slot; phase 4 sees
    # slots = 4 − 2 − 1 − 1 = 0 and the batch target waits — deliberate priority.
    assert counts["schedules"] == 1
    assert counts["batch_targets"] == 0
    assert (await _targets(session_factory, batch_id))[0].status == "pending"
    keyed = [c for c in await _calls(session_factory) if c.idempotency_key]
    assert [c.idempotency_key for c in keyed] == [f"sched:{sid}:{TODAY.isoformat()}"]

    # Once the schedule's call settles, the freed slot goes to the batch target.
    await _set_call(session_factory, keyed[0].id, status=CallStatus.COMPLETED)
    counts2 = await schedule_orchestrator.poll_once(session_factory, settings, now=NOW)
    assert counts2["batch_targets"] == 1
    assert (await _targets(session_factory, batch_id))[0].status == "materialized"


async def test_batch_target_skip_branches(session_factory):
    deleted = await _seed_elder(session_factory)
    bad_tz = await _seed_elder(session_factory)
    capped = await _seed_elder(session_factory)
    blocked = await _seed_elder(session_factory)
    batch_id = await _seed_running_batch(
        session_factory, elder_ids=[deleted.id, bad_tz.id, capped.id, blocked.id]
    )
    await _delete_elder(session_factory, deleted.id)  # FK SET NULL orphans target 0
    await _set_elder(session_factory, bad_tz.id, timezone="Not/AZone")
    for _ in range(2):  # default daily cap = 2: two reserved-key roots already on TODAY
        await _seed_call(
            session_factory,
            elder_id=capped.id,
            status=CallStatus.QUEUED,
            scheduled_at=NOW,
            key=f"sched:{uuid.uuid4()}:{TODAY.isoformat()}",
        )
    async with session_factory() as db:
        await dnc_repo.add_entry(db, blocked.phone_e164, "asked to stop")
        await db.commit()

    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    assert counts["batch_targets"] == 4
    targets = await _targets(session_factory, batch_id)
    assert (targets[0].status, targets[0].skip_reason) == ("skipped", "elder_deleted")
    assert (targets[1].status, targets[1].skip_reason) == ("skipped", "invalid_timezone")
    assert (targets[2].status, targets[2].skip_reason) == ("skipped", "daily_cap")
    # Spec §7: skips log at WARNING (fail-closed paths at ERROR), ids only.
    assert any("elder deleted" in m for m in warnings)
    assert any("daily autonomous-call cap" in m for m in warnings)
    # DNC: terminal row consumes the key; target materialized — the finalizer
    # settles it done/dnc_blocked next cycle (asserted in the finalizer matrix).
    assert targets[3].status == "materialized"
    assert targets[3].call_id is not None
    assert (await _get_call(session_factory, targets[3].call_id)).status is CallStatus.DNC_BLOCKED


async def test_batch_target_key_conflict_skipped(session_factory):
    owner = await _seed_elder(session_factory)
    foreign = await _seed_elder(session_factory)
    batch_id = await _seed_running_batch(session_factory, elder_ids=[owner.id])
    squatter_id = await _seed_call(
        session_factory, elder_id=foreign.id, scheduled_at=NOW, key=f"batch:{batch_id}:0"
    )

    errors: list[str] = []
    handler_id = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    targets = await _targets(session_factory, batch_id)
    assert (targets[0].status, targets[0].skip_reason) == ("skipped", "key_conflict")
    assert targets[0].call_id is None  # never linked to a foreign row
    calls = await _calls(session_factory)
    assert [c.id for c in calls] == [squatter_id]  # no second row minted
    assert calls[0].elder_id == foreign.id  # the foreign row untouched
    assert errors


async def test_batch_window_pushes_dial_time(session_factory):
    elder = await _seed_elder(session_factory)
    batch_id = await _seed_running_batch(
        session_factory,
        elder_ids=[elder.id],
        window_start=time(10, 0),
        window_end=time(12, 0),
        days_of_week=schedule_windows.days_to_mask(["mon"]),
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["batch_targets"] == 1
    targets = await _targets(session_factory, batch_id)
    assert targets[0].status == "materialized"
    assert targets[0].call_id is not None
    call = await _get_call(session_factory, targets[0].call_id)
    # NOW is Wednesday: the clamp is pushed into the batch window/day-mask —
    # next Monday 10:00 elder-local (EDT) = 14:00Z.
    assert call.scheduled_at == PUSHED_MONDAY_DIAL


async def test_pushed_target_capped_against_pushed_day(session_factory):
    elder = await _seed_elder(session_factory)
    # Two pre-existing autonomous roots already on the PUSHED Monday (elder-local).
    for hour in (14, 15):
        await _seed_call(
            session_factory,
            elder_id=elder.id,
            status=CallStatus.QUEUED,
            scheduled_at=datetime(2026, 6, 15, hour, 0, tzinfo=UTC),
            key=f"sched:{uuid.uuid4()}:2026-06-15",
        )
    batch_id = await _seed_running_batch(
        session_factory,
        elder_ids=[elder.id],
        window_start=time(10, 0),
        window_end=time(12, 0),
        days_of_week=schedule_windows.days_to_mask(["mon"]),
    )

    await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    # The cap is evaluated against the pushed dial day, never `now`'s day —
    # otherwise a window-pushed target dodges the harassment cap (TCPA hole).
    targets = await _targets(session_factory, batch_id)
    assert (targets[0].status, targets[0].skip_reason) == ("skipped", "daily_cap")
    assert len(await _calls(session_factory)) == 2  # only the pre-existing roots


async def test_crash_mid_batch_resume(session_factory):
    # §9 poller-crash shape: a cross-process duplicate already carries target 0's
    # key (same elder, chain root) while target 0 is still pending.
    elder_ids = [(await _seed_elder(session_factory)).id for _ in range(3)]
    batch_id = await _seed_running_batch(session_factory, elder_ids=elder_ids)
    orphan_id = await _seed_call(
        session_factory, elder_id=elder_ids[0], scheduled_at=NOW, key=f"batch:{batch_id}:0"
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["batch_targets"] == 3
    targets = await _targets(session_factory, batch_id)
    assert [t.status for t in targets] == ["materialized"] * 3
    assert targets[0].call_id == orphan_id  # replayed + linked, never duplicated
    calls = await _calls(session_factory)
    assert len(calls) == 3  # targets 1-2 fresh, target 0 adopted; no duplicates
    assert sorted(c.idempotency_key or "" for c in calls) == [
        f"batch:{batch_id}:{i}" for i in range(3)
    ]


async def test_finalizer_matrix(session_factory):
    elders = [await _seed_elder(session_factory) for _ in range(7)]
    batch_id = await _seed_running_batch(session_factory, elder_ids=[e.id for e in elders])
    targets = await _targets(session_factory, batch_id)

    # (a) completed root, no child -> settled done/completed.
    a_root = await _seed_call(session_factory, elder_id=elders[0].id, status=CallStatus.COMPLETED)
    # (b) no_answer root WITH a QUEUED child -> unsettled (stays materialized).
    b_root = await _seed_call(session_factory, elder_id=elders[1].id, status=CallStatus.NO_ANSWER)
    await _seed_call(
        session_factory,
        elder_id=elders[1].id,
        status=CallStatus.QUEUED,
        scheduled_at=NOW + timedelta(hours=1),
        parent_call_id=b_root,
        attempt=2,
    )
    # (c) ladder-exhausted no_answer: attempts 1 -> 2 -> 3, childless tip -> settled.
    c_root = await _seed_call(session_factory, elder_id=elders[2].id, status=CallStatus.NO_ANSWER)
    c_mid = await _seed_call(
        session_factory,
        elder_id=elders[2].id,
        status=CallStatus.NO_ANSWER,
        parent_call_id=c_root,
        attempt=2,
    )
    await _seed_call(
        session_factory,
        elder_id=elders[2].id,
        status=CallStatus.NO_ANSWER,
        parent_call_id=c_mid,
        attempt=3,
    )
    # (d) fail-closed-no-child: FAILED root, elder deleted => no child ever -> settled.
    d_root = await _seed_call(session_factory, elder_id=elders[3].id, status=CallStatus.FAILED)
    await _delete_elder(session_factory, elders[3].id)
    # (e) voicemail chain: root with child -> unsettled; childless attempt-2 -> settled.
    e1_root = await _seed_call(
        session_factory, elder_id=elders[4].id, status=CallStatus.VOICEMAIL_LEFT
    )
    await _seed_call(
        session_factory,
        elder_id=elders[4].id,
        status=CallStatus.QUEUED,
        scheduled_at=NOW + timedelta(hours=3),
        parent_call_id=e1_root,
        attempt=2,
    )
    e2_root = await _seed_call(
        session_factory, elder_id=elders[5].id, status=CallStatus.VOICEMAIL_LEFT
    )
    await _seed_call(
        session_factory,
        elder_id=elders[5].id,
        status=CallStatus.VOICEMAIL_LEFT,
        parent_call_id=e2_root,
        attempt=2,
    )
    # (f) DNC_BLOCKED root, no child -> settled done/dnc_blocked: completes the §9
    # DNC-batch flow (blocked number -> DNC_BLOCKED row -> target materialized -> done).
    f_root = await _seed_call(session_factory, elder_id=elders[6].id, status=CallStatus.DNC_BLOCKED)

    roots = [a_root, b_root, c_root, d_root, e1_root, e2_root, f_root]
    for target, root in zip(targets, roots, strict=True):
        await _set_target(
            session_factory, target.id, status="materialized", call_id=root, materialized_at=NOW
        )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["targets_finalized"] == 5
    by_index = {t.target_index: t for t in await _targets(session_factory, batch_id)}
    assert (by_index[0].status, by_index[0].final_status) == ("done", "completed")
    assert by_index[0].finalized_at == NOW
    assert (by_index[1].status, by_index[1].final_status) == ("materialized", None)
    assert (by_index[2].status, by_index[2].final_status) == ("done", "no_answer")
    assert (by_index[3].status, by_index[3].final_status) == ("done", "failed")
    assert (by_index[4].status, by_index[4].final_status) == ("materialized", None)
    assert (by_index[5].status, by_index[5].final_status) == ("done", "voicemail_left")
    assert (by_index[6].status, by_index[6].final_status) == ("done", "dnc_blocked")


async def test_sweep_cancels_queued_chains_backstop(session_factory):
    # The narrow cancel-vs-terminal-commit race: a cancelled batch still owns a
    # materialized target whose chain tip sits QUEUED.
    elders = [await _seed_elder(session_factory) for _ in range(2)]
    batch_id = await _seed_running_batch(
        session_factory, elder_ids=[e.id for e in elders], status="cancelled"
    )
    targets = await _targets(session_factory, batch_id)
    queued_root = await _seed_call(
        session_factory,
        elder_id=elders[0].id,
        status=CallStatus.QUEUED,
        scheduled_at=NOW + timedelta(minutes=30),
    )
    inflight_root = await _seed_call(
        session_factory, elder_id=elders[1].id, status=CallStatus.IN_PROGRESS, fresh=True
    )
    await _set_target(
        session_factory,
        targets[0].id,
        status="materialized",
        call_id=queued_root,
        materialized_at=NOW,
    )
    await _set_target(
        session_factory,
        targets[1].id,
        status="materialized",
        call_id=inflight_root,
        materialized_at=NOW,
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["chains_swept"] == 1
    assert (await _get_call(session_factory, queued_root)).status is CallStatus.CANCELLED
    # In-flight tips are never torn down — they finish naturally (spec §5.6).
    assert (await _get_call(session_factory, inflight_root)).status is CallStatus.IN_PROGRESS


async def test_completion_stamps_running_and_drained_cancelled(session_factory):
    # Running batch whose single chain completed.
    run_elder = await _seed_elder(session_factory)
    run_batch = await _seed_running_batch(session_factory, elder_ids=[run_elder.id])
    run_targets = await _targets(session_factory, run_batch)
    run_root = await _seed_call(session_factory, elder_id=run_elder.id, status=CallStatus.COMPLETED)
    await _set_target(
        session_factory,
        run_targets[0].id,
        status="materialized",
        call_id=run_root,
        materialized_at=NOW,
    )

    # Cancelled batch: one guard-cancelled chain + one that finished naturally after cancel.
    c_elders = [await _seed_elder(session_factory) for _ in range(2)]
    c_batch = await _seed_running_batch(
        session_factory, elder_ids=[e.id for e in c_elders], status="cancelled"
    )
    c_targets = await _targets(session_factory, c_batch)
    cancelled_root = await _seed_call(
        session_factory, elder_id=c_elders[0].id, status=CallStatus.CANCELLED
    )
    natural_root = await _seed_call(
        session_factory, elder_id=c_elders[1].id, status=CallStatus.NO_ANSWER
    )
    await _set_target(
        session_factory,
        c_targets[0].id,
        status="materialized",
        call_id=cancelled_root,
        materialized_at=NOW,
    )
    await _set_target(
        session_factory,
        c_targets[1].id,
        status="materialized",
        call_id=natural_root,
        materialized_at=NOW,
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["targets_finalized"] == 3
    assert counts["batches_completed"] == 2
    run_row = await _get_batch(session_factory, run_batch)
    assert (run_row.status, run_row.completed_at) == ("completed", NOW)
    c_row = await _get_batch(session_factory, c_batch)
    # Drained cancelled batch: completed_at stamped, status preserved.
    assert (c_row.status, c_row.completed_at) == ("cancelled", NOW)
    by_index = {t.target_index: t for t in await _targets(session_factory, c_batch)}
    assert (by_index[0].status, by_index[0].final_status) == ("done", "cancelled")
    assert (by_index[1].status, by_index[1].final_status) == ("done", "no_answer")  # truthful

    # Idempotent re-poll: stamps exactly once; drained batches leave the open
    # working set forever — phases 1/5 never revisit drained history (§9).
    counts2 = await schedule_orchestrator.poll_once(
        session_factory, _settings(), now=NOW + timedelta(minutes=5)
    )
    assert counts2["targets_finalized"] == 0
    assert counts2["batches_completed"] == 0
    assert (await _get_batch(session_factory, run_batch)).completed_at == NOW
    assert (await _get_batch(session_factory, c_batch)).completed_at == NOW
    async with session_factory() as db:
        assert await batches_repo.open_batches(db, limit=10) == []


# --- D8: policy × window composition at both materialization clamps (§3.3.3) ---


async def test_batch_target_policy_window_empty_skips_observably(session_factory):
    # §3.3.3 rule 2 (batch path): policy start 11:00 ∩ batch window 09:00-10:00
    # = ∅ -> the target is skipped observably (reason="window") — never
    # scheduled outside the window, never silently dropped, no call row. The
    # source="batch" x result="skipped_window" emission is the new bounded
    # label value (the custom_metrics docstring's old impossibility claim).
    elder = await _seed_elder(session_factory, policy={"quiet_hours_start_local": "11:00"})
    batch_id = await _seed_running_batch(
        session_factory,
        elder_ids=[elder.id],
        window_start=time(9, 0),
        window_end=time(10, 0),
        days_of_week=127,
    )
    before = counter_value(MATERIALIZED_CALLS_TOTAL, source="batch", result="skipped_window")

    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    finally:
        logger.remove(handler_id)

    assert counts["batch_targets"] == 1
    targets = await _targets(session_factory, batch_id)
    assert (targets[0].status, targets[0].skip_reason) == ("skipped", "window")
    assert targets[0].call_id is None
    assert await _calls(session_factory) == []
    after = counter_value(MATERIALIZED_CALLS_TOTAL, source="batch", result="skipped_window")
    assert after == before + 1
    assert warnings  # skips log at WARNING, ids only (spec §7)


async def test_batch_target_dial_pushed_to_policy_start_inside_window(session_factory):
    # §3.3.3 rule 1 (batch path): policy start 11:00 inside a 09:00-18:00 batch
    # window at 09:30 local — the dial lands AT the policy start (11:00 EDT =
    # 15:00Z), never inside the policy-forbidden [09:00, 11:00) zone.
    now = datetime(2026, 6, 10, 13, 30, tzinfo=UTC)  # 09:30 EDT
    elder = await _seed_elder(session_factory, policy={"quiet_hours_start_local": "11:00"})
    batch_id = await _seed_running_batch(
        session_factory,
        elder_ids=[elder.id],
        window_start=time(9, 0),
        window_end=time(18, 0),
        days_of_week=127,
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)

    assert counts["batch_targets"] == 1
    targets = await _targets(session_factory, batch_id)
    assert targets[0].status == "materialized"
    assert targets[0].call_id is not None
    call = await _get_call(session_factory, targets[0].call_id)
    assert call.scheduled_at == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)  # 11:00 EDT


async def test_schedule_occurrence_policy_window_empty_skips(session_factory):
    # §3.3.3 rule 2 (occurrence path): schedule window 09:00-10:00, policy start
    # 11:00, now 09:15 local on a masked weekday -> skipped_window, no call row,
    # and next_run_at advanced POLICY-FREE (cadence never sees policy bounds).
    now = datetime(2026, 6, 10, 13, 15, tzinfo=UTC)  # 09:15 EDT, Wednesday
    elder = await _seed_elder(session_factory, policy={"quiet_hours_start_local": "11:00"})
    sid = await _seed_schedule(
        session_factory,
        elder.id,
        next_run_at=now - timedelta(minutes=5),
        window_start=time(9, 0),
        window_end=time(10, 0),
    )

    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)
    finally:
        logger.remove(handler_id)

    assert counts["schedules"] == 1
    assert await _calls(session_factory) == []
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "skipped_window"
    assert schedule.last_materialized_date is None
    # Policy-free cadence: Thursday's STATUTORY window start, 09:00 EDT = 13:00Z.
    assert schedule.next_run_at == datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    assert warnings


async def test_schedule_occurrence_policy_end_clamp_past_skips(session_factory):
    # §3.3.3 rule 3 (clamp-before-skip): window 09:00-12:00 with policy END
    # 10:00. Companion at 09:45 local: inside the effective window -> created at
    # now; next_run_at advances policy-free.
    early = datetime(2026, 6, 10, 13, 45, tzinfo=UTC)  # 09:45 EDT
    elder_a = await _seed_elder(session_factory, policy={"quiet_hours_end_local": "10:00"})
    sid_a = await _seed_schedule(
        session_factory,
        elder_a.id,
        next_run_at=early - timedelta(minutes=5),
        window_start=time(9, 0),
        window_end=time(12, 0),
    )
    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=early)
    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    assert calls[0].scheduled_at == early
    sched_a = await _get_schedule(session_factory, sid_a)
    assert sched_a.last_result == "created"
    assert sched_a.next_run_at == datetime(2026, 6, 11, 13, 0, tzinfo=UTC)  # policy-free

    # At 10:30 local the STATUTORY window (until 12:00) is still open, but the
    # policy end (10:00) has passed: the skip keys on the EFFECTIVE end — a
    # clamp past it becomes skipped_window, never a late dial.
    late = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)  # 10:30 EDT
    elder_b = await _seed_elder(session_factory, policy={"quiet_hours_end_local": "10:00"})
    sid_b = await _seed_schedule(
        session_factory,
        elder_b.id,
        next_run_at=late - timedelta(minutes=5),
        window_start=time(9, 0),
        window_end=time(12, 0),
    )
    counts2 = await schedule_orchestrator.poll_once(session_factory, _settings(), now=late)
    assert counts2["schedules"] == 1
    assert len(await _calls(session_factory)) == 1  # no second call minted
    sched_b = await _get_schedule(session_factory, sid_b)
    assert sched_b.last_result == "skipped_window"
    assert sched_b.next_run_at == datetime(2026, 6, 11, 13, 0, tzinfo=UTC)  # policy-free


async def test_schedule_occurrence_policy_push_no_reschedule_loop(session_factory):
    # The re-claim-loop pin (plan executor note 5): claimed at its STATUTORY
    # next_run_at (09:00) under policy start 09:30. The staleness check keys on
    # the statutory window only, so the claim materializes exactly ONE call
    # clamped to the policy start (09:30 EDT = 13:30Z) and records `created` —
    # a naive policy-narrowed start_utc would take the `rescheduled` branch
    # with next_run_at(now) == now and re-claim the row every cycle until 09:30.
    now = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)  # exactly 09:00 EDT
    elder = await _seed_elder(session_factory, policy={"quiet_hours_start_local": "09:30"})
    sid = await _seed_schedule(
        session_factory,
        elder.id,
        next_run_at=now,
        window_start=time(9, 0),
        window_end=time(12, 0),
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    assert calls[0].scheduled_at == datetime(2026, 6, 10, 13, 30, tzinfo=UTC)  # 09:30 EDT
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "created"  # NOT "rescheduled"
    assert schedule.next_run_at == datetime(2026, 6, 11, 13, 0, tzinfo=UTC)

    # No re-claim loop: an immediate re-poll finds nothing due.
    counts2 = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)
    assert counts2["schedules"] == 0
    assert len(await _calls(session_factory)) == 1


async def test_windowless_batch_target_clamps_to_policy_start(session_factory):
    # Windowless batch + elder-profile policy: with no batch window there is no
    # next_run_at composition at all — the dial time is the bare policy-aware
    # next_allowed clamp, so a 09:30 EDT poll under policy start 11:00 schedules
    # the target at 11:00 EDT (15:00Z), never at now.
    now = datetime(2026, 6, 10, 13, 30, tzinfo=UTC)  # 09:30 EDT, Wednesday
    elder = await _seed_elder(session_factory, policy={"quiet_hours_start_local": "11:00"})
    batch_id = await _seed_running_batch(session_factory, elder_ids=[elder.id])  # no window

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)

    assert counts["batch_targets"] == 1
    targets = await _targets(session_factory, batch_id)
    assert targets[0].status == "materialized"
    assert targets[0].call_id is not None
    call = await _get_call(session_factory, targets[0].call_id)
    assert call.status is CallStatus.QUEUED
    assert call.scheduled_at == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)  # 11:00 EDT


async def test_schedule_profile_override_policy_clamps_materialized_call(session_factory):
    # profile_override threading pin (§3.3.2): the SCHEDULE's override carries
    # the narrowing (start 11:00) while the elder's assigned profile resolves
    # but has NO policy section. If only elder_profile_id were threaded, the
    # whole-profile walk would yield statutory and the call would dial at now
    # (09:30 EDT, inside [09:00, 17:00)); the override must win — the
    # materialized call clamps to the override's window start (11:00 EDT).
    now = datetime(2026, 6, 10, 13, 30, tzinfo=UTC)  # 09:30 EDT, Wednesday
    elder = await _seed_elder(session_factory, policy=None)  # published, no policy key
    async with session_factory() as db:
        override_pid = await _publish_policy_profile(
            db, policy={"quiet_hours_start_local": "11:00"}
        )
        await db.commit()
    sid = await _seed_schedule(
        session_factory,
        elder.id,
        next_run_at=now - timedelta(minutes=5),
        profile_override=override_pid,
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    assert calls[0].scheduled_at == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)  # 11:00 EDT
    assert calls[0].profile_override == override_pid
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "created"


async def test_batch_profile_override_policy_clamps_materialized_call(session_factory):
    # Same threading pin for the batch path: the BATCH-level override carries
    # the narrowing while the elder's profile has no policy section; the target
    # dial is pushed to the override's window start, never dialed at now.
    now = datetime(2026, 6, 10, 13, 30, tzinfo=UTC)  # 09:30 EDT, Wednesday
    elder = await _seed_elder(session_factory, policy=None)  # published, no policy key
    async with session_factory() as db:
        override_pid = await _publish_policy_profile(
            db, policy={"quiet_hours_start_local": "11:00"}
        )
        await db.commit()
    batch_id = await _seed_running_batch(
        session_factory,
        elder_ids=[elder.id],
        window_start=time(9, 0),
        window_end=time(18, 0),
        days_of_week=127,
        profile_override=override_pid,
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=now)

    assert counts["batch_targets"] == 1
    targets = await _targets(session_factory, batch_id)
    assert targets[0].status == "materialized"
    assert targets[0].call_id is not None
    call = await _get_call(session_factory, targets[0].call_id)
    assert call.scheduled_at == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)  # 11:00 EDT
    assert call.profile_override == override_pid


async def test_policy_free_profiles_orchestrate_unchanged(session_factory):
    # Ship-inert pin (spec §9): a resolving profile WITHOUT a `policy` section
    # reproduces the pre-policy happy path byte-for-byte (statutory defaults).
    elder = await _seed_elder(session_factory, policy=None)  # published, no policy key
    sid = await _seed_schedule(session_factory, elder.id, next_run_at=NOW - timedelta(hours=1))

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    call = calls[0]
    assert call.status is CallStatus.QUEUED
    assert call.idempotency_key == f"sched:{sid}:{TODAY.isoformat()}"
    assert call.scheduled_at == quiet_hours.next_allowed(NOW, "America/New_York")
    schedule = await _get_schedule(session_factory, sid)
    assert schedule.last_result == "created"
    assert schedule.last_materialized_date == TODAY
    assert schedule.next_run_at == NEXT_DAY_START
