"""US5 — per-slot (morning|evening) schedule materialization (spec §4.1 / Phase 7).

An contact may have one schedule per slot; each slot is its own ``call_schedules``
row with its own window/days/enabled flag and its own ``sched:{id}:{day}``
idempotency key, so the existing phase-3 materializer iterates them
independently. Pins the US5 independent test: two enabled slots -> two calls;
evening disabled -> only morning; contact on DNC -> neither dials (both blocked +
auto-disabled).
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import schedule_orchestrator
from usan_api.db.base import CallStatus
from usan_api.db.models import Call, CallSchedule
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.settings import Settings

# Wednesday 2026-06-10 15:00Z = 11:00 EDT — inside a 09:00-17:00 NY window for both slots.
NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
TODAY = date(2026, 6, 10)


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
                "TRUNCATE call_batch_targets, call_batches, call_schedules, calls, "
                "dnc_list, contacts CASCADE"
            )
        )
        await db.commit()


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


async def _seed_contact(factory):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="S", phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
    return contact


async def _seed_slot(factory, contact_id, *, slot, next_run_at, enabled=True):
    async with factory() as db:
        row = await schedules_repo.create_schedule(
            db,
            contact_id=contact_id,
            slot=slot,
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


async def _calls(factory):
    async with factory() as db:
        result = await db.execute(select(Call).order_by(Call.created_at, Call.id))
        return list(result.scalars().all())


async def test_both_slots_materialize_two_calls(session_factory):
    contact = await _seed_contact(session_factory)
    morning = await _seed_slot(
        session_factory, contact.id, slot="morning", next_run_at=NOW - timedelta(hours=2)
    )
    evening = await _seed_slot(
        session_factory, contact.id, slot="evening", next_run_at=NOW - timedelta(hours=1)
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 2
    calls = await _calls(session_factory)
    assert len(calls) == 2
    assert all(c.status is CallStatus.QUEUED for c in calls)
    # one call per slot, each keyed on its own schedule row id (per-slot, no collision).
    keys = {c.idempotency_key for c in calls}
    assert keys == {
        f"sched:{morning}:{TODAY.isoformat()}",
        f"sched:{evening}:{TODAY.isoformat()}",
    }


async def test_evening_disabled_only_morning_materializes(session_factory):
    contact = await _seed_contact(session_factory)
    morning = await _seed_slot(
        session_factory, contact.id, slot="morning", next_run_at=NOW - timedelta(hours=2)
    )
    await _seed_slot(
        session_factory,
        contact.id,
        slot="evening",
        enabled=False,
        next_run_at=NOW - timedelta(hours=1),
    )

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    # disabled evening is never claimed (WHERE enabled, the idx_call_schedules_due predicate).
    assert counts["schedules"] == 1
    calls = await _calls(session_factory)
    assert len(calls) == 1
    assert calls[0].idempotency_key == f"sched:{morning}:{TODAY.isoformat()}"


async def test_dnc_blocks_both_slots(session_factory):
    contact = await _seed_contact(session_factory)
    await _seed_slot(
        session_factory, contact.id, slot="morning", next_run_at=NOW - timedelta(hours=2)
    )
    await _seed_slot(
        session_factory, contact.id, slot="evening", next_run_at=NOW - timedelta(hours=1)
    )
    async with session_factory() as db:
        await dnc_repo.add_entry(db, contact.phone_e164, "asked to stop")
        await db.commit()

    counts = await schedule_orchestrator.poll_once(session_factory, _settings(), now=NOW)

    assert counts["schedules"] == 2
    calls = await _calls(session_factory)
    assert len(calls) == 2
    assert all(c.status is CallStatus.DNC_BLOCKED for c in calls)  # neither dials
    async with session_factory() as db:
        rows = list((await db.execute(select(CallSchedule))).scalars().all())
        assert {r.slot for r in rows} == {"morning", "evening"}
        assert all(r.enabled is False for r in rows)  # both auto-disabled (§5.3 step 3)
        assert all(r.last_result == "dnc_blocked" for r in rows)


async def test_daily_cap_bounds_total_roots_across_slots(session_factory):
    # US5 cap coupling (documented in settings.py): the per-contact daily cap bounds
    # TOTAL autonomous roots/day across BOTH slots. With cap=1 the earlier slot
    # (morning) dials and the evening is skipped_daily_cap — observably, and the
    # evening schedule stays enabled to retry next day (no silent drop, no
    # auto-disable). This pins the harassment-guard contract rather than forbidding
    # a cap of 1 (a valid single-slot ceiling).
    contact = await _seed_contact(session_factory)
    morning = await _seed_slot(
        session_factory, contact.id, slot="morning", next_run_at=NOW - timedelta(hours=2)
    )
    await _seed_slot(
        session_factory, contact.id, slot="evening", next_run_at=NOW - timedelta(hours=1)
    )

    counts = await schedule_orchestrator.poll_once(
        session_factory, _settings(MAX_AUTONOMOUS_CALLS_PER_CONTACT_PER_DAY="1"), now=NOW
    )

    assert counts["schedules"] == 2  # both rows claimed and processed
    calls = await _calls(session_factory)
    assert len(calls) == 1  # only the earlier slot materialized a dial
    assert calls[0].idempotency_key == f"sched:{morning}:{TODAY.isoformat()}"
    async with session_factory() as db:
        evening_row = await schedules_repo.get_by_contact_slot(
            db, contact_id=contact.id, slot="evening"
        )
        assert evening_row is not None
        assert evening_row.last_result == "skipped_daily_cap"
        assert evening_row.enabled is True  # stays enabled; retries next day
