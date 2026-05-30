import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory, *, status, room):
    # Unique phone per call: tests using this fixture share a long-lived Postgres
    # container with no truncation between them, so a fixed number would collide
    # on the phone_e164 unique constraint.
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=room,
        )
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_mark_answered_sets_in_progress(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r1")
    async with session_factory() as db:
        call = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_1")
        await db.commit()
    assert call.status is CallStatus.IN_PROGRESS
    assert call.answered_at is not None
    assert call.sip_call_id == "SCL_1"


@pytest.mark.asyncio
async def test_mark_dial_failure_sets_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r2")
    async with session_factory() as db:
        call = await calls_repo.mark_dial_failure(
            db, call_id, CallStatus.BUSY, end_reason="sip_busy", error={"sip_code": 486}
        )
        await db.commit()
    assert call.status is CallStatus.BUSY
    assert call.ended_at is not None
    assert call.end_reason == "sip_busy"
    assert call.error == {"sip_code": 486}


@pytest.mark.asyncio
async def test_mark_completed_only_when_in_progress(session_factory):
    # in_progress -> completed, with duration computed
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r3")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_3")
        await db.commit()
    async with session_factory() as db:
        call = await calls_repo.mark_completed_if_in_progress(db, "r3")
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.COMPLETED
    assert call.ended_at is not None
    assert call.duration_seconds is not None
    assert call.duration_seconds >= 0


@pytest.mark.asyncio
async def test_mark_completed_noop_when_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="r4")
    async with session_factory() as db:
        result = await calls_repo.mark_completed_if_in_progress(db, "r4")
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER  # unchanged


@pytest.mark.asyncio
async def test_mark_completed_unknown_room_is_none(session_factory):
    async with session_factory() as db:
        assert await calls_repo.mark_completed_if_in_progress(db, "nope") is None


@pytest.mark.asyncio
async def test_mark_voicemail_left_only_when_in_progress(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="vm1")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL")
        await db.commit()
    async with session_factory() as db:
        call = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.VOICEMAIL_LEFT
    assert call.end_reason == "voicemail"
    assert call.ended_at is not None
    assert call.duration_seconds is not None
    assert call.duration_seconds >= 0


@pytest.mark.asyncio
async def test_mark_voicemail_left_noop_when_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="vm2")
    async with session_factory() as db:
        result = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER
