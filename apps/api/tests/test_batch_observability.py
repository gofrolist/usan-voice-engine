"""Batch/scheduler observability (spec §7, §5.4(2)).

E1 scope: the six metric objects with bounded PHI-free labels, the gauges set
every retry-poller cycle regardless of flag state (pre-enable observability),
and `usan_dial_requeued_total{reason="quiet_hours"}` on the dial-time
stale-clamp re-queue path. E2 scope: the scheduler/batch-router counter
increments — materialization decisions per {source,result}, batch lifecycle
events, finalizer final_status outcomes, and the increment-after-commit
discipline (a crashed commit never counts, a recovery re-poll counts once).
"""

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS, counter_value, gauge_value
from usan_api import livekit_dispatch, retry_orchestrator, schedule_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatchTarget, CallSchedule, Elder
from usan_api.observability import custom_metrics
from usan_api.observability.custom_metrics import (
    BATCH_EVENTS_TOTAL,
    BATCH_TARGETS_FINALIZED_TOTAL,
    DIAL_REQUEUED_TOTAL,
    DIAL_SLOTS_FREE,
    IN_FLIGHT_CALLS,
    MATERIALIZED_CALLS_TOTAL,
)
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn
from usan_api.settings import Settings

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_x",
        "TELNYX_CALLER_ID": "+15551230000",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


def _label_names(counter) -> tuple[str, ...]:
    """The exact (ordered) label names of a Counter, via the public collect() API.

    Requires at least one labeled child to exist (the test creates one first).
    """
    (family,) = counter.collect()
    totals = [s for s in family.samples if s.name.endswith("_total")]
    assert totals, f"no _total samples on {family.name}"
    return tuple(totals[0].labels)


def test_metric_objects_and_bounded_labels():
    # Counter label names: exactly the spec §7 bounded sets, in order.
    MATERIALIZED_CALLS_TOTAL.labels(source="schedule", result="created")
    BATCH_EVENTS_TOTAL.labels(event="created")
    BATCH_TARGETS_FINALIZED_TOTAL.labels(final_status="completed")
    DIAL_REQUEUED_TOTAL.labels(reason="quiet_hours")
    assert _label_names(MATERIALIZED_CALLS_TOTAL) == ("source", "result")
    assert _label_names(BATCH_EVENTS_TOTAL) == ("event",)
    assert _label_names(BATCH_TARGETS_FINALIZED_TOTAL) == ("final_status",)
    assert _label_names(DIAL_REQUEUED_TOTAL) == ("reason",)
    with pytest.raises(ValueError, match="[Ii]ncorrect label names"):
        MATERIALIZED_CALLS_TOTAL.labels(origin="schedule", result="created")

    # Gauges are unlabeled: a single bare sample each.
    for gauge, name in (
        (IN_FLIGHT_CALLS, "usan_in_flight_calls"),
        (DIAL_SLOTS_FREE, "usan_dial_slots_free"),
    ):
        (family,) = gauge.collect()
        (sample,) = family.samples
        assert sample.name == name
        assert sample.labels == {}

    # Exposed sample names (Counter "usan_X" exposes "usan_X_total").
    body = generate_latest(REGISTRY).decode()
    for exposed in (
        "usan_materialized_calls_total",
        "usan_batch_events_total",
        "usan_batch_targets_finalized_total",
        "usan_dial_requeued_total",
        "usan_in_flight_calls",
        "usan_dial_slots_free",
    ):
        assert exposed in body

    # The module documents the structurally-impossible label combos (spec §7):
    # skipped_elder_deleted x schedule, rescheduled x batch — and that
    # skipped_window x batch IS emitted since the per-profile policy unlock
    # (policy ∩ window = ∅, small-unlocks spec §3.3.3 rule 2).
    doc = custom_metrics.__doc__ or ""
    assert "skipped_elder_deleted" in doc
    assert "skipped_window" in doc
    assert "rescheduled" in doc


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def clean_calls(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls CASCADE"))
        await db.commit()


async def _seed_call(
    factory, *, status, scheduled_at=None, updated_at=None, tz="UTC", room=None, elder_id=None
):
    """Insert one call (own elder unless given); optionally force updated_at afterwards."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        if elder_id is None:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=tz)
            elder_id = elder.id
        call = Call(
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=status,
            scheduled_at=scheduled_at,
            attempt=2,
            livekit_room=room if room is not None else f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        await db.commit()
        call_id = call.id
    if updated_at is not None:
        async with factory() as db:
            await db.execute(update(Call).where(Call.id == call_id).values(updated_at=updated_at))
            await db.commit()
    return call_id


@pytest.mark.asyncio
async def test_gauges_exported_every_retry_cycle_even_when_gate_and_scheduler_disabled(
    session_factory, clean_calls
):
    """Spec §5.4(2)/§7: the gauges live in the retry poller — the component that
    computes and enforces the gate — and are set every cycle even with
    CONCURRENCY_GATE_ENABLED=false and the scheduler poller disabled (defaults),
    so the dial-slot picture is truthful before the gate is ever enabled."""
    await _seed_call(session_factory, status=CallStatus.RINGING, updated_at=NOW)
    await _seed_call(session_factory, status=CallStatus.IN_PROGRESS, updated_at=NOW)

    claimed = await retry_orchestrator.poll_once(
        session_factory, _settings(CONCURRENCY_GATE_ENABLED="false"), now=NOW
    )

    assert claimed == []  # nothing due — the cycle still exports the gauges
    assert gauge_value(IN_FLIGHT_CALLS) == 2
    # Defaults: MAX_CONCURRENT_CALLS=8, RESERVED_CONCURRENCY=2.
    assert gauge_value(DIAL_SLOTS_FREE) == max(0, 8 - 2 - 2)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.mark.asyncio
async def test_dial_requeue_increments_quiet_hours_counter(
    monkeypatch, session_factory, clean_calls
):
    """The D5 stale-clamp scenario increments
    usan_dial_requeued_total{reason="quiet_hours"} after the re-queue commit;
    the inside-hours dial path never touches it."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=UTC)  # 23:00 EDT on 2026-06-09
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)

    call_id = await _seed_call(
        session_factory, status=CallStatus.DIALING, tz="America/New_York", room="usan-out-qh-m"
    )
    before = counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours")
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    assert counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours") == before + 1
    fake.agent_dispatch.create_dispatch.assert_not_awaited()

    # Inside quiet hours (12:00 EDT) the dial proceeds — counter unchanged.
    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)
    monkeypatch.setattr(
        livekit_dispatch, "_utcnow", lambda: datetime(2026, 6, 10, 16, 0, tzinfo=UTC)
    )
    call_id2 = await _seed_call(
        session_factory, status=CallStatus.DIALING, tz="America/New_York", room="usan-out-qh-ok-m"
    )
    await livekit_dispatch.dispatch_and_dial(call_id2, _settings())

    assert delegated == [call_id2]
    assert counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours") == before + 1


# --- E2: scheduler + batch-router counter increments (after commit) ---

# Wednesday 2026-06-10 15:00Z = 11:00 EDT — inside the default 09:00-17:00 NY window.
SCHED_NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
_TRUNCATE_SQL = (
    "TRUNCATE call_batch_targets, call_batches, call_schedules, calls, dnc_list, elders CASCADE"
)


@pytest.fixture
async def clean_scheduler_tables(session_factory):
    async with session_factory() as db:
        await db.execute(text(_TRUNCATE_SQL))
        await db.commit()


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
    window_start: time = time(9, 0),
    window_end: time = time(17, 0),
) -> uuid.UUID:
    async with factory() as db:
        row = await schedules_repo.create_schedule(
            db,
            elder_id=elder_id,
            window_start_local=window_start,
            window_end_local=window_end,
            days_of_week=127,
            enabled=True,
            dynamic_vars={},
            profile_override=None,
            next_run_at=SCHED_NOW - timedelta(hours=1),
        )
        await db.commit()
        return row.id


async def _seed_running_batch(factory, elder_ids: list[uuid.UUID]) -> uuid.UUID:
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="campaign",
            idempotency_key=None,
            payload_digest="d" * 64,
            trigger_at=None,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=None,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=eid) for eid in elder_ids],
        )
        batch.status = "running"
        batch.started_at = SCHED_NOW
        await db.commit()
        return batch.id


async def _with_factory(async_database_url: str, fn):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        return await fn(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _materialized(source: str, result: str) -> float:
    return counter_value(MATERIALIZED_CALLS_TOTAL, source=source, result=result)


@pytest.mark.asyncio
async def test_materialization_results_increment_counter(session_factory, clean_scheduler_tables):
    """Every D8 materialization decision lands in
    usan_materialized_calls_total{source,result}, incremented after the commit
    that made it true: created/skipped_window/dnc_blocked on the first poll,
    replayed on the re-claim, and a deleted-elder batch target as
    source="batch", result="skipped_elder_deleted"."""
    before = {
        r: _materialized("schedule", r)
        for r in ("created", "replayed", "skipped_window", "dnc_blocked")
    }
    before_batch = _materialized("batch", "skipped_elder_deleted")

    created_elder = await _seed_elder(session_factory)
    sid = await _seed_schedule(session_factory, created_elder.id)
    window_elder = await _seed_elder(session_factory)
    # 09:00-10:30 NY: SCHED_NOW (11:00 EDT) is past the window end.
    await _seed_schedule(session_factory, window_elder.id, window_end=time(10, 30))
    dnc_elder = await _seed_elder(session_factory)
    await _seed_schedule(session_factory, dnc_elder.id)
    async with session_factory() as db:
        await dnc_repo.add_entry(db, dnc_elder.phone_e164, "asked to stop")
        await db.commit()

    await schedule_orchestrator.poll_once(session_factory, _settings(), now=SCHED_NOW)

    assert _materialized("schedule", "created") == before["created"] + 1
    assert _materialized("schedule", "skipped_window") == before["skipped_window"] + 1
    assert _materialized("schedule", "dnc_blocked") == before["dnc_blocked"] + 1
    assert _materialized("schedule", "replayed") == before["replayed"]

    # Re-claim the created schedule with today's call already minted -> replayed.
    async with session_factory() as db:
        await db.execute(
            update(CallSchedule)
            .where(CallSchedule.id == sid)
            .values(next_run_at=SCHED_NOW - timedelta(hours=1))
        )
        await db.commit()
    await schedule_orchestrator.poll_once(session_factory, _settings(), now=SCHED_NOW)
    assert _materialized("schedule", "replayed") == before["replayed"] + 1
    assert _materialized("schedule", "created") == before["created"] + 1  # never re-counted

    # Batch plane: a deleted elder's target skips with source="batch".
    gone = await _seed_elder(session_factory)
    await _seed_running_batch(session_factory, [gone.id])
    async with session_factory() as db:
        await db.execute(delete(Elder).where(Elder.id == gone.id))
        await db.commit()
    await schedule_orchestrator.poll_once(session_factory, _settings(), now=SCHED_NOW)
    assert _materialized("batch", "skipped_elder_deleted") == before_batch + 1


def test_batch_events_increment_on_transitions(client, async_database_url):
    """usan_batch_events_total: created on POST /v1/batches, started on the
    poller trigger, completed on the drain, cancelled on the cancel endpoint —
    each after its commit. An idempotent re-cancel is not a transition and
    never double-counts."""

    async def _scrub(factory) -> None:
        async with factory() as db:
            await db.execute(text(_TRUNCATE_SQL))
            await db.commit()

    # Order-independence: the poll cycles below must only see this test's batches.
    asyncio.run(_with_factory(async_database_url, _scrub))

    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    created_elder = client.post(
        "/v1/elders",
        json={"name": "Rose", "phone_e164": phone, "timezone": "America/New_York"},
        headers=OPERATOR_HEADERS,
    )
    assert created_elder.status_code == 201
    elder_id = created_elder.json()["id"]

    before = {
        e: counter_value(BATCH_EVENTS_TOTAL, event=e)
        for e in ("created", "started", "completed", "cancelled")
    }
    created = client.post(
        "/v1/batches",
        json={"name": "June campaign", "targets": [{"elder_id": elder_id}]},
        headers=OPERATOR_HEADERS,
    )
    assert created.status_code == 201
    batch_id = created.json()["id"]
    assert counter_value(BATCH_EVENTS_TOTAL, event="created") == before["created"] + 1
    assert counter_value(BATCH_EVENTS_TOTAL, event="started") == before["started"]

    async def _trigger(factory) -> None:
        await schedule_orchestrator.poll_once(factory, _settings(), now=SCHED_NOW)

    asyncio.run(_with_factory(async_database_url, _trigger))
    assert counter_value(BATCH_EVENTS_TOTAL, event="started") == before["started"] + 1
    assert counter_value(BATCH_EVENTS_TOTAL, event="completed") == before["completed"]

    async def _settle_and_drain(factory) -> None:
        async with factory() as db:
            await db.execute(
                update(Call)
                .where(Call.idempotency_key == f"batch:{batch_id}:0")
                .values(status=CallStatus.COMPLETED)
            )
            await db.commit()
        await schedule_orchestrator.poll_once(factory, _settings(), now=SCHED_NOW)

    asyncio.run(_with_factory(async_database_url, _settle_and_drain))
    assert counter_value(BATCH_EVENTS_TOTAL, event="completed") == before["completed"] + 1

    second = client.post(
        "/v1/batches",
        json={"name": "second", "targets": [{"elder_id": elder_id}]},
        headers=OPERATOR_HEADERS,
    )
    assert second.status_code == 201
    second_id = second.json()["id"]
    assert (
        client.post(f"/v1/batches/{second_id}/cancel", headers=OPERATOR_HEADERS).status_code == 200
    )
    assert counter_value(BATCH_EVENTS_TOTAL, event="cancelled") == before["cancelled"] + 1
    # Idempotent re-cancel: 200 unchanged, and NOT a second lifecycle transition.
    assert (
        client.post(f"/v1/batches/{second_id}/cancel", headers=OPERATOR_HEADERS).status_code == 200
    )
    assert counter_value(BATCH_EVENTS_TOTAL, event="cancelled") == before["cancelled"] + 1


@pytest.mark.asyncio
async def test_finalizer_increments_final_status(session_factory, clean_scheduler_tables):
    """Settled chains land in usan_batch_targets_finalized_total{final_status}:
    a completed chain, and — closing the §9 DNC-batch flow through the metric —
    a settled DNC_BLOCKED chain (D9 finalizer-matrix case f)."""
    completed_elder = await _seed_elder(session_factory)
    dnc_elder = await _seed_elder(session_factory)
    batch_id = await _seed_running_batch(session_factory, [completed_elder.id, dnc_elder.id])
    completed_root = await _seed_call(
        session_factory, status=CallStatus.COMPLETED, elder_id=completed_elder.id
    )
    dnc_root = await _seed_call(
        session_factory, status=CallStatus.DNC_BLOCKED, elder_id=dnc_elder.id
    )
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        for target, root in zip(targets, [completed_root, dnc_root], strict=True):
            await db.execute(
                update(CallBatchTarget)
                .where(CallBatchTarget.id == target.id)
                .values(status="materialized", call_id=root, materialized_at=SCHED_NOW)
            )
        await db.commit()
    before_completed = counter_value(BATCH_TARGETS_FINALIZED_TOTAL, final_status="completed")
    before_dnc = counter_value(BATCH_TARGETS_FINALIZED_TOTAL, final_status="dnc_blocked")

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=SCHED_NOW)

    assert counts["targets_finalized"] == 2
    assert (
        counter_value(BATCH_TARGETS_FINALIZED_TOTAL, final_status="completed")
        == before_completed + 1
    )
    assert (
        counter_value(BATCH_TARGETS_FINALIZED_TOTAL, final_status="dnc_blocked") == before_dnc + 1
    )


def _exploding_on_schedule_commit(real_factory):
    """A session factory whose first schedule-materialization commit raises once
    — the crash window between the call-insert/bookkeeping writes and their
    commit. The claimed CallSchedule identifies that commit: it is strongly
    referenced by the phase-3 loop, while the flushed-clean Call may already
    have been dropped from the session's weak-referencing identity map."""
    state = {"armed": True}

    def make():
        session = real_factory()
        real_commit = session.commit

        async def commit() -> None:
            if state["armed"] and any(
                isinstance(obj, CallSchedule) for obj in session.sync_session
            ):
                state["armed"] = False
                raise RuntimeError("simulated crash at the materialization commit")
            await real_commit()

        session.commit = commit
        return session

    return make


@pytest.mark.asyncio
async def test_increments_happen_after_commit(session_factory, clean_scheduler_tables):
    """Phase-3 discipline (spec §7): the counter mirrors COMMITTED transitions
    only. A materialization whose commit crashes must not count; the recovery
    re-poll counts the row exactly once — a crash can never double-count."""
    elder = await _seed_elder(session_factory)
    await _seed_schedule(session_factory, elder.id)
    before_created = _materialized("schedule", "created")
    before_replayed = _materialized("schedule", "replayed")

    with pytest.raises(RuntimeError, match="simulated crash"):
        await schedule_orchestrator.poll_once(
            _exploding_on_schedule_commit(session_factory), _settings(), now=SCHED_NOW
        )

    assert _materialized("schedule", "created") == before_created  # nothing committed

    # The rolled-back claim is still due; the healthy re-poll counts it ONCE.
    await schedule_orchestrator.poll_once(session_factory, _settings(), now=SCHED_NOW)
    assert _materialized("schedule", "created") == before_created + 1
    assert _materialized("schedule", "replayed") == before_replayed
