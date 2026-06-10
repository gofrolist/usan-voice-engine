import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

FIXED_NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)  # inside [09:00, 21:00) UTC


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(calls_repo, "_utcnow", lambda: FIXED_NOW)


async def _seed_terminal(factory, *, status, attempt=1, timezone="UTC", dynamic_vars=None):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=timezone)
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            dynamic_vars=dynamic_vars or {},
            livekit_room="usan-outbound-parent",
        )
        # create_call defaults attempt via the model; set explicitly for the test
        call.attempt = attempt
        await db.flush()
        await db.commit()
        return call.id, elder.id


async def _child_count(factory, parent_id) -> int:
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_schedule_retry_creates_child(session_factory):
    parent_id, elder_id = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, dynamic_vars={"k": "v"}
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    # re-read in a fresh session to prove it persisted
    async with session_factory() as db:
        reloaded = await calls_repo.get_call(db, child.id)
    assert reloaded is not None
    assert reloaded.parent_call_id == parent_id
    assert reloaded.attempt == 2
    assert reloaded.status is CallStatus.QUEUED
    assert reloaded.elder_id == elder_id
    assert reloaded.dynamic_vars == {"k": "v"}
    assert reloaded.idempotency_key is None
    assert reloaded.livekit_room.startswith("usan-outbound-")
    assert reloaded.livekit_room != "usan-outbound-parent"
    assert reloaded.scheduled_at is not None
    assert reloaded.scheduled_at.tzinfo is not None
    # no_answer attempt 1 -> +30min, inside the UTC window -> exact
    assert reloaded.scheduled_at == FIXED_NOW + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_schedule_retry_is_idempotent(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.BUSY, attempt=1)
    async with session_factory() as db:
        first = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    async with session_factory() as db:
        second = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert first is not None
    assert second is None
    assert await _child_count(session_factory, parent_id) == 1


@pytest.mark.asyncio
async def test_schedule_retry_stops_at_policy_cap(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.NO_ANSWER, attempt=3)
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_noop_for_non_retryable_status(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.COMPLETED, attempt=1)
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_when_elder_missing(session_factory):
    # elder_id is ON DELETE SET NULL, so a parent can legitimately have no elder.
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.FAILED, attempt=1)
    async with session_factory() as db:
        parent = await calls_repo.get_call(db, parent_id)
        parent.elder_id = None
        await db.commit()
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_for_missing_parent(session_factory):
    async with session_factory() as db:
        assert await calls_repo.schedule_retry(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_schedule_retry_fails_closed_on_bad_timezone(session_factory):
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, timezone="Not/AZone"
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_child_inherits_profile_override(session_factory):
    # profile_override is live (runtime agent-config + SMS template resolution),
    # so attempts 2..n must keep it instead of silently reverting to the default
    # profile (spec §2.3(3)/§6.1).
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, dynamic_vars={"k": "v"}
    )
    async with session_factory() as db:
        profile = await profiles_repo.create_profile(
            db, name=f"retry-override-{uuid.uuid4()}", description=None, actor_email="t@usan.test"
        )
        await profiles_repo.publish(db, profile.id, note=None, actor_email="t@usan.test")
        parent = await calls_repo.get_call(db, parent_id)
        parent.profile_override = profile.id
        await db.commit()
        profile_id = profile.id
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    # re-read in a fresh session to prove it persisted
    async with session_factory() as db:
        reloaded = await calls_repo.get_call(db, child.id)
    assert reloaded is not None
    assert reloaded.profile_override == profile_id
    # regression: dynamic_vars still copied alongside the override
    assert reloaded.dynamic_vars == {"k": "v"}


@pytest.mark.asyncio
async def test_schedule_retry_clamps_into_quiet_hours(session_factory):
    # Eastern elder; FIXED_NOW 12:00 UTC == 08:00 EDT (before 09:00 EDT).
    # voicemail_left attempt 1 -> +3h == 15:00 UTC == 11:00 EDT (now inside window) -> exact.
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.VOICEMAIL_LEFT,
        attempt=1,
        timezone="America/New_York",
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(hours=3)
