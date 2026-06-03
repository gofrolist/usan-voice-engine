import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import background, retry_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate_calls(session_factory):
    from sqlalchemy import text

    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls CASCADE"))
        await db.commit()


async def _seed(factory, *, status, scheduled_at):
    """Insert one call."""
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
        return call.id


@pytest.mark.asyncio
async def test_claim_due_retries_claims_due_queued(session_factory):
    due = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    async with session_factory() as db:
        claimed = await calls_repo.claim_due_retries(db, now=NOW, limit=10)
        await db.commit()
    assert claimed == [due]
    async with session_factory() as db:
        call = await calls_repo.get_call(db, due)
    assert call.status is CallStatus.DIALING


@pytest.mark.asyncio
async def test_claim_skips_not_yet_due(session_factory):
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=NOW + timedelta(hours=1))
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_skips_null_scheduled_queued(session_factory):
    # Initial calls (scheduled_at IS NULL) must NEVER be claimed by the poller.
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=None)
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_skips_non_queued(session_factory):
    await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=1))
    await _seed(
        session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=NOW - timedelta(minutes=1)
    )
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_respects_limit_and_order(session_factory):
    older = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=10)
    )
    newer = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    third = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=5)
    )
    async with session_factory() as db:
        claimed = await calls_repo.claim_due_retries(db, now=NOW, limit=2)
        await db.commit()
    # earliest scheduled_at first; the 2 earliest are `older` then `third`
    assert claimed == [older, third]
    async with session_factory() as db:
        leftover = await calls_repo.get_call(db, newer)
    assert leftover.status is CallStatus.QUEUED  # third row not claimed


@pytest.mark.asyncio
async def test_claim_skips_locked_rows(session_factory, async_database_url):
    due = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        async with session_factory() as db_a:
            claimed_a = await calls_repo.claim_due_retries(db_a, now=NOW, limit=10)
            assert due in claimed_a  # A holds the row lock (not committed)
            async with factory_b() as db_b:
                claimed_b = await calls_repo.claim_due_retries(db_b, now=NOW, limit=10)
                assert due not in claimed_b  # B skips the locked row instead of blocking
            await db_a.commit()
    finally:
        await engine_b.dispose()


async def _force_updated_at(factory, call_id, when):
    from sqlalchemy import update

    async with factory() as db:
        await db.execute(update(Call).where(Call.id == call_id).values(updated_at=when))
        await db.commit()


@pytest.mark.asyncio
async def test_reclaim_requeues_stale_dialing(session_factory):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=600))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(db, now=NOW, stale_after_s=300, limit=10)
        await db.commit()
    assert reclaimed == [cid]
    async with session_factory() as db:
        call = await calls_repo.get_call(db, cid)
    assert call.status is CallStatus.QUEUED  # now re-claimable by the poller


@pytest.mark.asyncio
async def test_reclaim_leaves_fresh_dialing_alone(session_factory):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=10))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(db, now=NOW, stale_after_s=300, limit=10)
        await db.commit()
    assert reclaimed == []
    async with session_factory() as db:
        call = await calls_repo.get_call(db, cid)
    assert call.status is CallStatus.DIALING


@pytest.mark.asyncio
async def test_reclaim_ignores_null_scheduled_and_in_progress(session_factory):
    # A stranded INITIAL call (scheduled_at NULL) is the caller's to re-enqueue.
    initial = await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=None)
    await _force_updated_at(session_factory, initial, NOW - timedelta(seconds=600))
    # An answered call is IN_PROGRESS, not DIALING.
    answered = await _seed(
        session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, answered, NOW - timedelta(seconds=600))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(db, now=NOW, stale_after_s=300, limit=10)
        await db.commit()
    assert reclaimed == []


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


@pytest.mark.asyncio
async def test_poll_once_claims_commits_and_dispatches(session_factory, monkeypatch):
    due1 = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=2)
    )
    due2 = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )

    dispatched: list[uuid.UUID] = []

    async def _fake_dispatch(call_id, settings):
        dispatched.append(call_id)

    monkeypatch.setattr(retry_orchestrator.livekit_dispatch, "dispatch_and_dial", _fake_dispatch)

    ids = await retry_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    await background.drain(timeout=5)

    assert set(ids) == {due1, due2}
    assert set(dispatched) == {due1, due2}
    # claim was committed before dispatch: rows are DIALING in a fresh session
    async with session_factory() as db:
        for cid in (due1, due2):
            call = await calls_repo.get_call(db, cid)
            assert call.status is CallStatus.DIALING

    # Ensure updated_at is within the stale window so the reaper does not
    # re-queue these freshly-committed DIALING rows when NOW is ahead of the
    # real DB clock (the default stale threshold is 300 s).
    for cid in (due1, due2):
        await _force_updated_at(session_factory, cid, NOW)

    # a second poll claims nothing (rows already DIALING)
    ids2 = await retry_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    assert ids2 == []


@pytest.mark.asyncio
async def test_poll_once_reaps_then_claims_stuck_row(session_factory, monkeypatch):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=600))

    dispatched: list[uuid.UUID] = []

    async def _fake_dispatch(call_id, settings):
        dispatched.append(call_id)

    monkeypatch.setattr(retry_orchestrator.livekit_dispatch, "dispatch_and_dial", _fake_dispatch)

    ids = await retry_orchestrator.poll_once(
        session_factory, _settings(RETRY_STUCK_DIALING_S="300"), now=NOW
    )
    await background.drain(timeout=5)
    # the stranded row was reaped to QUEUED, then claimed and dispatched in one cycle
    assert ids == [cid]
    assert dispatched == [cid]


@pytest.mark.asyncio
async def test_run_poller_exits_promptly_when_stop_preset(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)
    calls_count = {"n": 0}

    async def _fake_poll(factory, settings, *, now=None):
        calls_count["n"] += 1
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(retry_orchestrator.run_poller(_settings(), stop), timeout=2)
    assert calls_count["n"] == 0  # stop preset -> never polls, never sleeps the interval


@pytest.mark.asyncio
async def test_run_poller_survives_poll_exception(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)
    stop = asyncio.Event()
    n = {"i": 0}

    async def _flaky(factory, settings, *, now=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("transient boom")
        stop.set()
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _flaky)

    settings = _settings()
    settings.retry_poll_interval_s = 0.01  # Settings has no validate_assignment; spin fast
    await asyncio.wait_for(retry_orchestrator.run_poller(settings, stop), timeout=2)
    assert n["i"] >= 2  # exception in cycle 1 did not kill the loop


@pytest.mark.asyncio
async def test_run_poller_stop_interrupts_interval_sleep(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)

    async def _fake_poll(factory, settings, *, now=None):
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    settings = _settings()  # default 30s interval
    task = asyncio.create_task(retry_orchestrator.run_poller(settings, stop))
    await asyncio.sleep(0.05)  # let one cycle run and enter the interval sleep
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # must return well before 30s
