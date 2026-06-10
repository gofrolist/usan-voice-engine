"""Batch/scheduler observability (spec §7, §5.4(2)).

E1 scope: the six metric objects with bounded PHI-free labels, the gauges set
every retry-poller cycle regardless of flag state (pre-enable observability),
and `usan_dial_requeued_total{reason="quiet_hours"}` on the dial-time
stale-clamp re-queue path. E2 appends the scheduler/batch-router counter
increments.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import counter_value, gauge_value
from usan_api import livekit_dispatch, retry_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.observability import custom_metrics
from usan_api.observability.custom_metrics import (
    BATCH_EVENTS_TOTAL,
    BATCH_TARGETS_FINALIZED_TOTAL,
    DIAL_REQUEUED_TOTAL,
    DIAL_SLOTS_FREE,
    IN_FLIGHT_CALLS,
    MATERIALIZED_CALLS_TOTAL,
)
from usan_api.repositories import elders as elders_repo
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
    # skipped_elder_deleted x schedule, skipped_window/rescheduled x batch.
    doc = custom_metrics.__doc__ or ""
    assert "skipped_elder_deleted" in doc
    assert "skipped_window" in doc
    assert "rescheduled" in doc


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def clean_calls(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls CASCADE"))
        await db.commit()


async def _seed_call(factory, *, status, scheduled_at=None, updated_at=None, tz="UTC", room=None):
    """Insert one call (own elder); optionally force updated_at afterwards."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=tz)
        call = Call(
            elder_id=elder.id,
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
