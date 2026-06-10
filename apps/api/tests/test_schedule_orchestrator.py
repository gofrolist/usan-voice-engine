"""Scheduler poller phases 2+3 + loop: trigger due batches, materialize due schedules.

Pins the exhaustive spec §5.2 phase-3 branch matrix — created / replayed /
rescheduled / skipped_window / skipped_invalid_timezone / dnc_blocked /
key_conflict — where EVERY branch writes ``last_result`` AND advances
``next_run_at`` (omitting the advance on any branch is the §5.3(5)/§9 infinite
re-claim loop), the phase-2 batch trigger, open-transaction SKIP LOCKED
disjointness, and the run_poller loop discipline cloned from the retry poller.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from loguru import logger
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import quiet_hours, schedule_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch, CallSchedule, Elder
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


async def _seed_elder(factory, *, timezone: str = "America/New_York") -> Elder:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="S", phone_e164=phone, timezone=timezone)
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
            profile_override=None,
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
