"""follow_up_flags repository: create + filtered list (mirrors wellness repo)."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import follow_up_flags as repo


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
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Flag Contact", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
        )
        await db.commit()
        return call.id, contact.id


async def test_create_and_list_follow_up_flag(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await repo.create_follow_up_flag(
            db,
            call_id=cid,
            contact_id=eid,
            severity="urgent",
            category="medical",
            reason="chest pain reported",
        )
        await db.commit()
        assert isinstance(row.id, int)
        assert row.status == "open"
        assert row.severity == "urgent"

        all_flags = await repo.list_flags(db)
        assert any(f.id == row.id for (f, _, _) in all_flags)

        by_contact = await repo.list_flags(db, contact_id=eid)
        assert [f.id for (f, _, _) in by_contact] == [row.id]

        open_only = await repo.list_flags(db, status="open")
        assert any(f.id == row.id for (f, _, _) in open_only)
        # The repo keeps accepting arbitrary status strings as a filter — the
        # typed Literal (422 on junk) lands at the router (spec §4.4).
        closed = await repo.list_flags(db, status="closed")
        assert all(f.id != row.id for (f, _, _) in closed)


async def test_list_flags_respects_limit(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        for _ in range(2):
            await repo.create_follow_up_flag(
                db,
                call_id=cid,
                contact_id=eid,
                severity="routine",
                category="other",
                reason=None,
            )
        await db.commit()

        limited = await repo.list_flags(db, contact_id=eid, limit=1)
        assert len(limited) == 1


async def _create_flag(db, cid, eid, *, severity="routine"):
    return await repo.create_follow_up_flag(
        db, call_id=cid, contact_id=eid, severity=severity, category="other", reason=None
    )


async def test_get_flag_returns_row_or_none(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await _create_flag(db, cid, eid)
        await db.commit()

        found = await repo.get_flag(db, row.id)
        assert found is not None
        assert found.id == row.id
        assert found.status == "open"

        assert await repo.get_flag(db, row.id + 999_999) is None


async def test_update_status_guarded_state_machine(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        flag = await _create_flag(db, cid, eid, severity="urgent")
        fresh = await _create_flag(db, cid, eid)
        acked = await _create_flag(db, cid, eid)
        await db.commit()

        # open -> acknowledged: returns the updated row with the workflow stamps set.
        row = await repo.update_status(
            db, flag.id, new_status="acknowledged", actor_email="nurse@usan.org"
        )
        assert row is not None
        assert row.status == "acknowledged"
        assert row.status_updated_at is not None
        assert row.status_updated_by == "nurse@usan.org"

        # acknowledged -> resolved succeeds.
        row = await repo.update_status(
            db, flag.id, new_status="resolved", actor_email="resolver@usan.org"
        )
        assert row is not None
        assert row.status == "resolved"
        assert row.status_updated_by == "resolver@usan.org"

        # A fresh row may skip straight open -> resolved.
        row = await repo.update_status(
            db, fresh.id, new_status="resolved", actor_email="nurse@usan.org"
        )
        assert row is not None
        assert row.status == "resolved"

        # Backward resolved -> acknowledged: None, row unchanged (WHERE IS the state machine).
        denied = await repo.update_status(
            db, flag.id, new_status="acknowledged", actor_email="other@usan.org"
        )
        assert denied is None
        unchanged = await repo.get_flag(db, flag.id)
        assert unchanged is not None
        assert unchanged.status == "resolved"
        assert unchanged.status_updated_by == "resolver@usan.org"

        # Same-status acknowledged -> acknowledged: None, status_updated_* untouched
        # (caller disambiguates no-op vs 409 via get_flag).
        row = await repo.update_status(
            db, acked.id, new_status="acknowledged", actor_email="nurse@usan.org"
        )
        assert row is not None
        first_stamp = row.status_updated_at
        noop = await repo.update_status(
            db, acked.id, new_status="acknowledged", actor_email="other@usan.org"
        )
        assert noop is None
        untouched = await repo.get_flag(db, acked.id)
        assert untouched is not None
        assert untouched.status == "acknowledged"
        assert untouched.status_updated_at == first_stamp
        assert untouched.status_updated_by == "nurse@usan.org"
        await db.commit()


async def test_list_flags_returns_contact_join_tuples(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        row = await _create_flag(db, cid, eid)
        await db.commit()

        contact = await contacts_repo.get_contact(db, eid)
        assert contact is not None

        [(flag, contact_name, phone)] = await repo.list_flags(db, contact_id=eid)
        assert flag.id == row.id
        assert contact_name == "Flag Contact"
        assert phone == contact.phone_e164


async def test_list_flags_severity_filter_and_urgent_first(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        urgent = await _create_flag(db, cid, eid, severity="urgent")
        routine = [await _create_flag(db, cid, eid) for _ in range(5)]
        await db.commit()

        urgent_only = await repo.list_flags(db, severity="urgent")
        assert [f.id for (f, _, _) in urgent_only] == [urgent.id]
        routine_only = await repo.list_flags(db, severity="routine")
        assert {f.id for (f, _, _) in routine_only} == {f.id for f in routine}

        # The urgent flag is OLDER than a full page of routine flags yet sorts
        # first: (severity='urgent') DESC, created_at DESC, id DESC.
        page = [f.id for (f, _, _) in await repo.list_flags(db, limit=5)]
        assert page[0] == urgent.id
        assert page[1:] == [f.id for f in reversed(routine)][:4]


async def test_list_flags_offset(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        flags = [await _create_flag(db, cid, eid) for _ in range(3)]
        await db.commit()
        newest_first = [f.id for f in reversed(flags)]

        page0 = await repo.list_flags(db, contact_id=eid)
        assert [f.id for (f, _, _) in page0] == newest_first
        page1 = await repo.list_flags(db, contact_id=eid, offset=1)
        assert [f.id for (f, _, _) in page1] == newest_first[1:]


async def test_count_by_status_groups(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        flags = [await _create_flag(db, cid, eid) for _ in range(4)]
        await db.commit()

        # Absent statuses are omitted, not reported as 0 (pinned shape).
        assert await repo.count_by_status(db) == {"open": 4}

        assert (
            await repo.update_status(
                db, flags[0].id, new_status="acknowledged", actor_email="nurse@usan.org"
            )
            is not None
        )
        assert (
            await repo.update_status(
                db, flags[1].id, new_status="resolved", actor_email="nurse@usan.org"
            )
            is not None
        )
        await db.commit()

        assert await repo.count_by_status(db) == {"open": 2, "acknowledged": 1, "resolved": 1}


async def test_count_open_urgent(session_factory):
    cid, eid = await _seed_call_and_contact(session_factory)
    async with session_factory() as db:
        await _create_flag(db, cid, eid, severity="urgent")
        urgent2 = await _create_flag(db, cid, eid, severity="urgent")
        await _create_flag(db, cid, eid, severity="routine")
        await db.commit()

        assert await repo.count_open_urgent(db) == 2

        # Only status='open' AND severity='urgent' counts.
        assert (
            await repo.update_status(
                db, urgent2.id, new_status="resolved", actor_email="nurse@usan.org"
            )
            is not None
        )
        await db.commit()
        assert await repo.count_open_urgent(db) == 1
