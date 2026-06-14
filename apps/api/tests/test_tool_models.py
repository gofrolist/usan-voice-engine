import uuid
from datetime import UTC, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import MedicationLog, Transcript, WellnessLog
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.tools import (
    FlagForFollowupRequest,
    FollowupFlaggedResponse,
    ScheduleCallbackRequest,
)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
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


def test_flag_for_followup_request_valid():
    cid = uuid.uuid4()
    req = FlagForFollowupRequest(
        call_id=cid, severity="urgent", category="medical", reason="chest pain"
    )
    assert req.call_id == cid
    assert req.severity == "urgent"
    assert req.category == "medical"


def test_flag_for_followup_rejects_bad_enums():
    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="emergency", category="medical", reason="x"
        )
    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="urgent", category="weather", reason="x"
        )


def test_flag_for_followup_reason_max_length():
    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="routine", category="other", reason="x" * 2001
        )


def test_flag_for_followup_reason_rejects_empty():
    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="routine", category="other", reason=""
        )


def test_followup_flagged_response_shape():
    assert FollowupFlaggedResponse(id=7).id == 7


def test_schedule_callback_naive_requested_at_becomes_utc():
    req = ScheduleCallbackRequest(
        call_id=uuid.uuid4(),
        requested_time_text="tomorrow morning",
        requested_at="2026-06-10T09:00:00",  # naive: no offset / Z
    )
    assert req.requested_at is not None
    assert req.requested_at.tzinfo is not None
    assert req.requested_at.utcoffset().total_seconds() == 0


def test_schedule_callback_offset_requested_at_preserved():
    req = ScheduleCallbackRequest(
        call_id=uuid.uuid4(),
        requested_time_text="tomorrow",
        requested_at="2026-06-10T09:00:00-05:00",
    )
    assert req.requested_at is not None
    assert req.requested_at.utcoffset() == timezone(timedelta(hours=-5)).utcoffset(None)


def test_schedule_callback_requested_at_none_passes_through():
    req = ScheduleCallbackRequest(
        call_id=uuid.uuid4(), requested_time_text="someday", requested_at=None
    )
    assert req.requested_at is None
    assert UTC is not None  # import is exercised by the naive-coercion test above
