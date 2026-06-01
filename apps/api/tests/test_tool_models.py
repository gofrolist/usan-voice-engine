import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import MedicationLog, Transcript, WellnessLog
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            livekit_room="usan-outbound-tm",
        )
        await db.commit()
        return call.id, elder.id


@pytest.mark.asyncio
async def test_wellness_log_round_trip(session_factory):
    call_id, elder_id = await _seed_call(session_factory)
    async with session_factory() as db:
        row = WellnessLog(call_id=call_id, elder_id=elder_id, mood=4, pain_level=2, notes="ok")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(WellnessLog, row_id)
    assert got is not None
    assert got.mood == 4
    assert got.pain_level == 2
    assert got.notes == "ok"
    assert got.raw == {}
    assert got.logged_at is not None


@pytest.mark.asyncio
async def test_medication_log_round_trip(session_factory):
    call_id, elder_id = await _seed_call(session_factory)
    async with session_factory() as db:
        row = MedicationLog(
            call_id=call_id, elder_id=elder_id, medication_name="Aspirin", taken=True
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(MedicationLog, row_id)
    assert got is not None
    assert got.medication_name == "Aspirin"
    assert got.taken is True
    assert got.reported_time is None


@pytest.mark.asyncio
async def test_transcript_round_trip(session_factory):
    call_id, _ = await _seed_call(session_factory)
    async with session_factory() as db:
        row = Transcript(
            call_id=call_id, role="user", content="hello", started_at=calls_repo._utcnow()
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(Transcript, row_id)
    assert got is not None
    assert got.role == "user"
    assert got.content == "hello"
    assert got.tool_name is None
    assert got.created_at is not None
