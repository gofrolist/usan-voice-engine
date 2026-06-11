"""Global concurrency gate in retry_orchestrator.poll_once (spec §5.4, §9 flag matrix).

Pins: the recency-bounded in-flight count, the gate's claim-limit shrink, the
zero-slot claim skip, the AUTONOMOUS_DIALING_PAUSED emergency stop, gate-disabled
bit-identical behavior, and the count+claim single-transaction invariant.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import background, retry_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
# The gate's recency bound: outbound_max_call_duration_s (default 1800) + 120.
MAX_AGE_S = 1800 + 120


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate_calls(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls CASCADE"))
        await db.commit()


@pytest.fixture(autouse=True)
def _clear_background_tasks():
    background._tasks.clear()
    yield
    background._tasks.clear()


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


async def _seed(factory, *, status, scheduled_at, updated_at=None):
    """Insert one call (own elder); optionally force updated_at afterwards."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            scheduled_at=scheduled_at,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
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


async def _status_of(factory, call_id) -> CallStatus:
    async with factory() as db:
        call = await calls_repo.get_call(db, call_id)
        assert call is not None
        return call.status


def _patch_dispatch(monkeypatch, dispatched: list[uuid.UUID]) -> None:
    async def _fake_dispatch(call_id, settings):
        dispatched.append(call_id)

    monkeypatch.setattr(retry_orchestrator.livekit_dispatch, "dispatch_and_dial", _fake_dispatch)


@pytest.mark.asyncio
async def test_count_in_flight_recency_bounded(session_factory):
    # Three fresh dial-slot consumers: DIALING, RINGING, IN_PROGRESS.
    await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.RINGING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=None, updated_at=NOW)
    # A wedged IN_PROGRESS row (lost webhook): older than the max-call-duration
    # ceiling. Without the recency bound, eight of these would silently halt ALL
    # autonomous dialing forever (spec §5.4(1)).
    await _seed(
        session_factory,
        status=CallStatus.IN_PROGRESS,
        scheduled_at=None,
        updated_at=NOW - timedelta(seconds=MAX_AGE_S + 60),
    )
    # Non-in-flight statuses never consume a slot.
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=NOW, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.COMPLETED, scheduled_at=None, updated_at=NOW)

    async with session_factory() as db:
        assert await calls_repo.count_in_flight(db, now=NOW, max_age_s=MAX_AGE_S) == 3


@pytest.mark.asyncio
async def test_count_queued_due_matches_claim_predicate(session_factory):
    # Exactly the idx_calls_due_retries predicate: status='queued' AND scheduled_at <= now.
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1))
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=NOW + timedelta(hours=1))
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=None)
    await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=1))

    async with session_factory() as db:
        assert await calls_repo.count_queued_due(db, now=NOW) == 1


@pytest.mark.asyncio
async def test_gate_shrinks_claim_limit(session_factory, monkeypatch):
    # 5 max - 2 reserved - 2 in-flight = 1 free slot.
    await _seed(session_factory, status=CallStatus.RINGING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=None, updated_at=NOW)
    earliest = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=10)
    )
    others = [
        await _seed(
            session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=m)
        )
        for m in (5, 3, 1)
    ]
    dispatched: list[uuid.UUID] = []
    _patch_dispatch(monkeypatch, dispatched)

    claimed = await retry_orchestrator.poll_once(
        session_factory,
        _settings(
            CONCURRENCY_GATE_ENABLED="true",
            MAX_CONCURRENT_CALLS="5",
            RESERVED_CONCURRENCY="2",
        ),
        now=NOW,
    )
    await background.drain(timeout=5)

    assert claimed == [earliest]
    assert dispatched == [earliest]
    for cid in others:
        assert await _status_of(session_factory, cid) is CallStatus.QUEUED


@pytest.mark.asyncio
async def test_gate_zero_slots_claims_nothing(session_factory, monkeypatch):
    # 5 max - 2 reserved - 3 in-flight = 0 free slots: the claim is skipped entirely.
    await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.RINGING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=None, updated_at=NOW)
    due = [
        await _seed(
            session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=m)
        )
        for m in (2, 1)
    ]

    async def _explode_claim(db, *, now, limit):
        raise AssertionError("claim_due_retries must not be called with zero free slots")

    monkeypatch.setattr(calls_repo, "claim_due_retries", _explode_claim)

    claimed = await retry_orchestrator.poll_once(
        session_factory,
        _settings(
            CONCURRENCY_GATE_ENABLED="true",
            MAX_CONCURRENT_CALLS="5",
            RESERVED_CONCURRENCY="2",
        ),
        now=NOW,
    )

    assert claimed == []
    for cid in due:
        assert await _status_of(session_factory, cid) is CallStatus.QUEUED


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["true", "false"])
async def test_paused_claims_nothing_preserving_state(session_factory, monkeypatch, gate):
    # The reversible emergency stop (spec §5.4(3)): zero claims, state untouched,
    # WARNING emitted — distinct from RETRY_POLLER_ENABLED=false (which would also
    # kill stuck-DIALING reclaim and crash-marking).
    due = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    dispatched: list[uuid.UUID] = []
    _patch_dispatch(monkeypatch, dispatched)
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        claimed = await retry_orchestrator.poll_once(
            session_factory,
            _settings(AUTONOMOUS_DIALING_PAUSED="true", CONCURRENCY_GATE_ENABLED=gate),
            now=NOW,
        )
    finally:
        logger.remove(handler_id)

    assert claimed == []
    assert dispatched == []
    assert await _status_of(session_factory, due) is CallStatus.QUEUED
    assert any("paused" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_gate_disabled_is_bit_identical_to_today(session_factory, monkeypatch):
    # Gate off + in-flight rows present: claims min(retry_batch_size, all_due)
    # exactly as the pre-gate code (ship-inert proof, spec §5.1/§10.1). With the
    # gate ON these settings would yield 0 free slots and claim nothing.
    await _seed(session_factory, status=CallStatus.RINGING, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=None, updated_at=NOW)
    await _seed(session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=None, updated_at=NOW)
    first = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=10)
    )
    second = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=5)
    )
    third = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    dispatched: list[uuid.UUID] = []
    _patch_dispatch(monkeypatch, dispatched)

    claimed = await retry_orchestrator.poll_once(
        session_factory,
        _settings(
            CONCURRENCY_GATE_ENABLED="false",
            MAX_CONCURRENT_CALLS="5",
            RESERVED_CONCURRENCY="2",
            RETRY_BATCH_SIZE="2",
        ),
        now=NOW,
    )
    await background.drain(timeout=5)

    assert claimed == [first, second]  # min(retry_batch_size=2, 3 due), earliest first
    assert await _status_of(session_factory, third) is CallStatus.QUEUED


@pytest.mark.asyncio
async def test_count_and_claim_share_one_transaction(session_factory, monkeypatch):
    # Count + claim must share one session/transaction snapshot — separate
    # transactions would add avoidable intra-process drift from webhooks/ad-hoc
    # dials racing between them (spec §5.4).
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1))
    dispatched: list[uuid.UUID] = []
    _patch_dispatch(monkeypatch, dispatched)

    sessions: dict[str, object] = {}
    real_count = calls_repo.count_in_flight
    real_claim = calls_repo.claim_due_retries

    async def _spy_count(db, **kwargs):
        sessions["count"] = db
        return await real_count(db, **kwargs)

    async def _spy_claim(db, **kwargs):
        sessions["claim"] = db
        return await real_claim(db, **kwargs)

    monkeypatch.setattr(calls_repo, "count_in_flight", _spy_count)
    monkeypatch.setattr(calls_repo, "claim_due_retries", _spy_claim)

    claimed = await retry_orchestrator.poll_once(
        session_factory, _settings(CONCURRENCY_GATE_ENABLED="true"), now=NOW
    )
    await background.drain(timeout=5)

    assert len(claimed) == 1
    assert sessions["count"] is sessions["claim"]
