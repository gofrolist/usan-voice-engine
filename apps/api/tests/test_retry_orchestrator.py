import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

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


async def _seed(factory, *, status, scheduled_at, updated_offset_s=None):
    """Insert one call. If updated_offset_s is set, force updated_at to NOW + offset."""
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
