"""call_schedules repository: CRUD + SKIP LOCKED claim + last_result bookkeeping."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import CallSchedule
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import contacts as contacts_repo

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE call_batch_targets, call_batches, call_schedules, calls, contacts CASCADE"
            )
        )
        await db.commit()


async def _seed_contact(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Schedule Contact", phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
        return contact.id


async def _create_schedule(
    factory,
    contact_id: uuid.UUID,
    *,
    next_run_at: datetime,
    enabled: bool = True,
) -> uuid.UUID:
    async with factory() as db:
        row = await schedules_repo.create_schedule(
            db,
            contact_id=contact_id,
            window_start_local=time(9, 0),
            window_end_local=time(17, 0),
            days_of_week=127,
            enabled=enabled,
            dynamic_vars={},
            profile_override=None,
            next_run_at=next_run_at,
        )
        await db.commit()
        return row.id


async def test_create_get_and_unique_contact(session_factory):
    contact_id = await _seed_contact(session_factory)
    async with session_factory() as db:
        row = await schedules_repo.create_schedule(
            db,
            contact_id=contact_id,
            window_start_local=time(9, 0),
            window_end_local=time(17, 0),
            days_of_week=127,
            enabled=True,
            dynamic_vars={"first_name": "Rose"},
            profile_override=None,
            next_run_at=NOW + timedelta(hours=1),
        )
        await db.commit()
        schedule_id = row.id
        assert row.enabled is True
        assert row.days_of_week == 127
        assert row.dynamic_vars == {"first_name": "Rose"}
        assert row.last_result is None
        assert row.last_materialized_date is None

    async with session_factory() as db:
        fetched = await schedules_repo.get_schedule(db, schedule_id)
        assert fetched is not None
        assert fetched.id == schedule_id
        assert fetched.slot == "morning"  # US5 default
        by_contact = await schedules_repo.get_by_contact(db, contact_id)
        assert [s.id for s in by_contact] == [schedule_id]  # one slot so far
        morning = await schedules_repo.get_by_contact_slot(
            db, contact_id=contact_id, slot="morning"
        )
        assert morning is not None
        assert morning.id == schedule_id
        assert (
            await schedules_repo.get_by_contact_slot(db, contact_id=contact_id, slot="evening")
            is None
        )
        assert await schedules_repo.get_schedule(db, uuid.uuid4()) is None

    # UNIQUE (contact_id, slot): a second schedule for the same contact+slot (default
    # 'morning') is rejected — the router maps this to 409.
    async with session_factory() as db:
        with pytest.raises(IntegrityError):
            await schedules_repo.create_schedule(
                db,
                contact_id=contact_id,
                window_start_local=time(10, 0),
                window_end_local=time(16, 0),
                days_of_week=31,
                enabled=True,
                dynamic_vars={},
                profile_override=None,
                next_run_at=NOW + timedelta(hours=2),
            )

    # US5: a different slot for the SAME contact IS allowed (independent evening call).
    async with session_factory() as db:
        evening = await schedules_repo.create_schedule(
            db,
            contact_id=contact_id,
            slot="evening",
            window_start_local=time(18, 0),
            window_end_local=time(20, 0),
            days_of_week=127,
            enabled=True,
            dynamic_vars={},
            profile_override=None,
            next_run_at=NOW + timedelta(hours=6),
        )
        await db.commit()
        evening_id = evening.id
    async with session_factory() as db:
        both = await schedules_repo.get_by_contact(db, contact_id)
        assert {s.slot for s in both} == {"morning", "evening"}
        assert {s.id for s in both} == {schedule_id, evening_id}

    async with session_factory() as db:
        fetched = await schedules_repo.get_schedule(db, schedule_id)
        assert fetched is not None
        await schedules_repo.delete_schedule(db, fetched)
        await db.commit()
    async with session_factory() as db:
        assert await schedules_repo.get_schedule(db, schedule_id) is None


async def test_claim_due_schedules_orders_and_skips(session_factory):
    e1 = await _seed_contact(session_factory)
    e2 = await _seed_contact(session_factory)
    e3 = await _seed_contact(session_factory)
    e4 = await _seed_contact(session_factory)
    due_old = await _create_schedule(session_factory, e1, next_run_at=NOW - timedelta(minutes=10))
    due_new = await _create_schedule(session_factory, e2, next_run_at=NOW - timedelta(minutes=1))
    await _create_schedule(session_factory, e3, next_run_at=NOW + timedelta(hours=1))  # future
    # Disabled: never claimed even though due (WHERE enabled, the
    # idx_call_schedules_due partial-index predicate).
    await _create_schedule(
        session_factory, e4, next_run_at=NOW - timedelta(minutes=5), enabled=False
    )

    async with session_factory() as db:
        claimed = await schedules_repo.claim_due_schedules(db, now=NOW, limit=10)
        assert [s.id for s in claimed] == [due_old, due_new]  # ordered by next_run_at
        await db.commit()

    # limit respected: only the earliest due row is claimed
    async with session_factory() as db:
        claimed = await schedules_repo.claim_due_schedules(db, now=NOW, limit=1)
        assert [s.id for s in claimed] == [due_old]


async def test_claim_skip_locked_disjoint(session_factory, async_database_url):
    e1 = await _seed_contact(session_factory)
    e2 = await _seed_contact(session_factory)
    first = await _create_schedule(session_factory, e1, next_run_at=NOW - timedelta(minutes=10))
    second = await _create_schedule(session_factory, e2, next_run_at=NOW - timedelta(minutes=1))

    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        async with session_factory() as db_a:
            claimed_a = await schedules_repo.claim_due_schedules(db_a, now=NOW, limit=1)
            assert [s.id for s in claimed_a] == [first]  # A holds the row lock (no commit)
            async with factory_b() as db_b:
                claimed_b = await schedules_repo.claim_due_schedules(db_b, now=NOW, limit=10)
                # B skips A's locked row instead of blocking; claims are disjoint.
                assert [s.id for s in claimed_b] == [second]
            await db_a.commit()
    finally:
        await engine_b.dispose()


async def test_record_result_writes_bookkeeping(session_factory):
    contact_id = await _seed_contact(session_factory)
    schedule_id = await _create_schedule(session_factory, contact_id, next_run_at=NOW)

    next_run = NOW + timedelta(days=1)
    async with session_factory() as db:
        schedule = await schedules_repo.get_schedule(db, schedule_id)
        assert schedule is not None
        await schedules_repo.record_result(
            db,
            schedule,
            result="skipped_window",
            now=NOW,
            next_run_at=next_run,
            last_materialized_date=date(2026, 6, 10),
        )
        await db.commit()

    async with session_factory() as db:
        row = await schedules_repo.get_schedule(db, schedule_id)
        assert row is not None
        assert row.last_result == "skipped_window"
        assert row.last_result_at == NOW
        assert row.next_run_at == next_run  # advanced
        assert row.last_materialized_date == date(2026, 6, 10)
        assert row.enabled is True  # untouched unless asked

    # The DNC auto-disable write path: enabled=False flips the switch; omitted
    # kwargs leave next_run_at/last_materialized_date untouched.
    later = NOW + timedelta(minutes=5)
    async with session_factory() as db:
        schedule = await schedules_repo.get_schedule(db, schedule_id)
        assert schedule is not None
        await schedules_repo.record_result(
            db, schedule, result="dnc_blocked", now=later, enabled=False
        )
        await db.commit()

    async with session_factory() as db:
        row = await schedules_repo.get_schedule(db, schedule_id)
        assert row is not None
        assert row.enabled is False
        assert row.last_result == "dnc_blocked"
        assert row.last_result_at == later
        assert row.next_run_at == next_run  # unchanged
        assert row.last_materialized_date == date(2026, 6, 10)  # unchanged


async def test_list_filters_last_result_and_contact(session_factory):
    assert schedules_repo.MAX_SCHEDULES_LIMIT == 500  # spec §4.1 bounded read

    e1 = await _seed_contact(session_factory)
    e2 = await _seed_contact(session_factory)
    e3 = await _seed_contact(session_factory)
    s1 = await _create_schedule(session_factory, e1, next_run_at=NOW)
    s2 = await _create_schedule(session_factory, e2, next_run_at=NOW)
    s3 = await _create_schedule(session_factory, e3, next_run_at=NOW)

    async with session_factory() as db:
        for sid, result in ((s1, "skipped_window"), (s2, "skipped_window"), (s3, "created")):
            schedule = await schedules_repo.get_schedule(db, sid)
            assert schedule is not None
            await schedules_repo.record_result(db, schedule, result=result, now=NOW)
        # Force identical created_at so ordering must fall back to the id tiebreaker.
        await db.execute(
            update(CallSchedule).where(CallSchedule.id.in_([s1, s2, s3])).values(created_at=NOW)
        )
        await db.commit()

    # Python UUID ordering matches Postgres uuid byte ordering, so id DESC is
    # computable client-side.
    expected_skipped = sorted([s1, s2], reverse=True)
    all_expected = sorted([s1, s2, s3], reverse=True)

    async with session_factory() as db:
        rows = await schedules_repo.list_schedules(db, last_result="skipped_window")
        assert [r.id for r in rows] == expected_skipped  # only the misses, newest-first

        mine = await schedules_repo.list_schedules(db, contact_id=e3)
        assert [r.id for r in mine] == [s3]

        none = await schedules_repo.list_schedules(db, contact_id=e3, last_result="skipped_window")
        assert none == []

        # limit clamps low (0 -> 1) and high (10_000 -> MAX_SCHEDULES_LIMIT);
        # offset pages past the first row.
        first_page = await schedules_repo.list_schedules(db, limit=0)
        assert [r.id for r in first_page] == all_expected[:1]
        rest = await schedules_repo.list_schedules(db, limit=10_000, offset=1)
        assert [r.id for r in rest] == all_expected[1:]
