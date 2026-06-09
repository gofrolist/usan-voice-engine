"""callback_requests repository: create + filtered list (mirrors follow_up_flags repo)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import callback_requests as cb_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call_and_elder(factory) -> tuple[uuid.UUID, uuid.UUID]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Callback Elder", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
        )
        await db.commit()
        return call.id, elder.id


async def test_create_callback_request_persists_row(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            elder_id=elder_id,
            requested_time_text="tomorrow afternoon",
            requested_at=when,
            notes="prefers a call back",
        )
        await db.commit()
        assert isinstance(row.id, int)
        assert row.call_id == call_id
        assert row.elder_id == elder_id
        assert row.requested_time_text == "tomorrow afternoon"
        assert row.requested_at == when
        assert row.notes == "prefers a call back"
        assert row.status == "open"


async def test_create_callback_request_allows_null_requested_at(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            elder_id=elder_id,
            requested_time_text="sometime soon",
            requested_at=None,
            notes=None,
        )
        await db.commit()
        assert row.requested_at is None
        assert row.notes is None


async def test_list_callback_requests_filters_by_status(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            elder_id=elder_id,
            requested_time_text="first",
            requested_at=None,
            notes=None,
        )
        await db.commit()

        # Scope reads to this elder so shared session-container state can't bleed in.
        open_rows = await cb_repo.list_callback_requests(
            db, status="open", elder_id=elder_id, limit=50
        )
        assert [r.id for r in open_rows] == [row.id]

        resolved_rows = await cb_repo.list_callback_requests(
            db, status="resolved", elder_id=elder_id, limit=50
        )
        assert resolved_rows == []


async def test_list_callback_requests_filters_by_elder(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    _, other_elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            elder_id=elder_id,
            requested_time_text="mine",
            requested_at=None,
            notes=None,
        )
        await db.commit()

        mine = await cb_repo.list_callback_requests(db, elder_id=elder_id)
        assert [r.id for r in mine] == [row.id]

        theirs = await cb_repo.list_callback_requests(db, elder_id=other_elder_id)
        assert theirs == []


async def test_list_callback_requests_respects_limit(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        for _ in range(2):
            await cb_repo.create_callback_request(
                db,
                call_id=call_id,
                elder_id=elder_id,
                requested_time_text="repeat",
                requested_at=None,
                notes=None,
            )
        await db.commit()

        limited = await cb_repo.list_callback_requests(db, elder_id=elder_id, limit=1)
        assert len(limited) == 1
