"""callback_requests repository: create + filtered list (mirrors follow_up_flags repo)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
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


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Module isolation (test_call_schedules_repo.py precedent): exact global
    # GROUP-BY count assertions would otherwise be flaky — earlier tests in this
    # module (and siblings sharing the session container) accumulate open rows.
    async with session_factory() as db:
        await db.execute(text("TRUNCATE follow_up_flags, callback_requests, calls, elders CASCADE"))
        await db.commit()


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


async def _create_request(db, call_id, elder_id):
    return await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="tomorrow",
        requested_at=None,
        notes=None,
    )


async def test_get_request_returns_row_or_none(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        row = await _create_request(db, call_id, elder_id)
        await db.commit()

        found = await cb_repo.get_request(db, row.id)
        assert found is not None
        assert found.id == row.id
        assert found.status == "open"

        assert await cb_repo.get_request(db, row.id + 999_999) is None


async def test_update_status_guarded_state_machine(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        request = await _create_request(db, call_id, elder_id)
        fresh = await _create_request(db, call_id, elder_id)
        acked = await _create_request(db, call_id, elder_id)
        await db.commit()

        # open -> acknowledged: returns the updated row with the workflow stamps set.
        row = await cb_repo.update_status(
            db, request.id, new_status="acknowledged", actor_email="nurse@usan.org"
        )
        assert row is not None
        assert row.status == "acknowledged"
        assert row.status_updated_at is not None
        assert row.status_updated_by == "nurse@usan.org"

        # acknowledged -> resolved succeeds.
        row = await cb_repo.update_status(
            db, request.id, new_status="resolved", actor_email="resolver@usan.org"
        )
        assert row is not None
        assert row.status == "resolved"
        assert row.status_updated_by == "resolver@usan.org"

        # A fresh row may skip straight open -> resolved.
        row = await cb_repo.update_status(
            db, fresh.id, new_status="resolved", actor_email="nurse@usan.org"
        )
        assert row is not None
        assert row.status == "resolved"

        # Backward resolved -> acknowledged: None, row unchanged (WHERE IS the state machine).
        denied = await cb_repo.update_status(
            db, request.id, new_status="acknowledged", actor_email="other@usan.org"
        )
        assert denied is None
        unchanged = await cb_repo.get_request(db, request.id)
        assert unchanged is not None
        assert unchanged.status == "resolved"
        assert unchanged.status_updated_by == "resolver@usan.org"

        # Same-status acknowledged -> acknowledged: None, status_updated_* untouched
        # (caller disambiguates no-op vs 409 via get_request).
        row = await cb_repo.update_status(
            db, acked.id, new_status="acknowledged", actor_email="nurse@usan.org"
        )
        assert row is not None
        first_stamp = row.status_updated_at
        noop = await cb_repo.update_status(
            db, acked.id, new_status="acknowledged", actor_email="other@usan.org"
        )
        assert noop is None
        untouched = await cb_repo.get_request(db, acked.id)
        assert untouched is not None
        assert untouched.status == "acknowledged"
        assert untouched.status_updated_at == first_stamp
        assert untouched.status_updated_by == "nurse@usan.org"
        await db.commit()


async def test_count_by_status_groups(session_factory):
    call_id, elder_id = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        rows = [await _create_request(db, call_id, elder_id) for _ in range(4)]
        await db.commit()

        # Absent statuses are omitted, not reported as 0 (pinned shape).
        assert await cb_repo.count_by_status(db) == {"open": 4}

        assert (
            await cb_repo.update_status(
                db, rows[0].id, new_status="acknowledged", actor_email="nurse@usan.org"
            )
            is not None
        )
        assert (
            await cb_repo.update_status(
                db, rows[1].id, new_status="resolved", actor_email="nurse@usan.org"
            )
            is not None
        )
        await db.commit()

        assert await cb_repo.count_by_status(db) == {"open": 2, "acknowledged": 1, "resolved": 1}
