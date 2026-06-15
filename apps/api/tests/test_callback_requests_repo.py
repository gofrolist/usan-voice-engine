"""callback_requests repository: create + filtered list (mirrors follow_up_flags repo)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import callback_requests as cb_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Module isolation (test_call_schedules_repo.py precedent): exact global
    # GROUP-BY count assertions would otherwise be flaky — earlier tests in this
    # module (and siblings sharing the session container) accumulate open rows.
    async with session_factory() as db:
        await db.execute(
            text("TRUNCATE follow_up_flags, callback_requests, calls, contacts CASCADE")
        )
        await db.commit()


async def _seed_call_and_contact(factory) -> tuple[uuid.UUID, uuid.UUID]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Callback Contact", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
        )
        await db.commit()
        return call.id, contact.id


async def test_create_callback_request_persists_row(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            contact_id=contact_id,
            requested_time_text="tomorrow afternoon",
            requested_at=when,
            notes="prefers a call back",
        )
        await db.commit()
        assert isinstance(row.id, int)
        assert row.call_id == call_id
        assert row.contact_id == contact_id
        assert row.requested_time_text == "tomorrow afternoon"
        assert row.requested_at == when
        assert row.notes == "prefers a call back"
        assert row.status == "open"


async def test_create_callback_request_allows_null_requested_at(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            contact_id=contact_id,
            requested_time_text="sometime soon",
            requested_at=None,
            notes=None,
        )
        await db.commit()
        assert row.requested_at is None
        assert row.notes is None


async def test_list_callback_requests_filters_by_status(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            contact_id=contact_id,
            requested_time_text="first",
            requested_at=None,
            notes=None,
        )
        await db.commit()

        # Scope reads to this contact so shared session-container state can't bleed in.
        open_rows = await cb_repo.list_callback_requests(
            db, status="open", contact_id=contact_id, limit=50
        )
        assert [r.id for (r, _, _) in open_rows] == [row.id]

        resolved_rows = await cb_repo.list_callback_requests(
            db, status="resolved", contact_id=contact_id, limit=50
        )
        assert resolved_rows == []


async def test_list_callback_requests_filters_by_contact(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    _, other_contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await cb_repo.create_callback_request(
            db,
            call_id=call_id,
            contact_id=contact_id,
            requested_time_text="mine",
            requested_at=None,
            notes=None,
        )
        await db.commit()

        mine = await cb_repo.list_callback_requests(db, contact_id=contact_id)
        assert [r.id for (r, _, _) in mine] == [row.id]

        theirs = await cb_repo.list_callback_requests(db, contact_id=other_contact_id)
        assert theirs == []


async def test_list_callback_requests_respects_limit(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        for _ in range(2):
            await cb_repo.create_callback_request(
                db,
                call_id=call_id,
                contact_id=contact_id,
                requested_time_text="repeat",
                requested_at=None,
                notes=None,
            )
        await db.commit()

        limited = await cb_repo.list_callback_requests(db, contact_id=contact_id, limit=1)
        assert len(limited) == 1


async def _create_request(db, call_id, contact_id):
    return await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        contact_id=contact_id,
        requested_time_text="tomorrow",
        requested_at=None,
        notes=None,
    )


async def test_get_request_returns_row_or_none(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await _create_request(db, call_id, contact_id)
        await db.commit()

        found = await cb_repo.get_request(db, row.id)
        assert found is not None
        assert found.id == row.id
        assert found.status == "open"

        assert await cb_repo.get_request(db, row.id + 999_999) is None


async def test_update_status_guarded_state_machine(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        request = await _create_request(db, call_id, contact_id)
        fresh = await _create_request(db, call_id, contact_id)
        acked = await _create_request(db, call_id, contact_id)
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


async def test_list_callback_requests_returns_contact_join_tuples(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await _create_request(db, call_id, contact_id)
        await db.commit()

        contact = await contacts_repo.get_contact(db, contact_id)
        assert contact is not None

        [(req, contact_name, phone)] = await cb_repo.list_callback_requests(
            db, contact_id=contact_id
        )
        assert req.id == row.id
        assert contact_name == "Callback Contact"
        assert phone == contact.phone_e164


async def test_list_callback_requests_offset_newest_first(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        rows = [await _create_request(db, call_id, contact_id) for _ in range(3)]
        await db.commit()
        newest_first = [r.id for r in reversed(rows)]

        # Ordering unchanged by C3: still newest-first (no urgent-first here).
        page0 = await cb_repo.list_callback_requests(db, contact_id=contact_id)
        assert [r.id for (r, _, _) in page0] == newest_first
        page1 = await cb_repo.list_callback_requests(db, contact_id=contact_id, offset=1)
        assert [r.id for (r, _, _) in page1] == newest_first[1:]


async def test_count_by_status_groups(session_factory):
    call_id, contact_id = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        rows = [await _create_request(db, call_id, contact_id) for _ in range(4)]
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
