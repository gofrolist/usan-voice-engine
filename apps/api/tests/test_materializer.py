"""Shared materializer: schedule_orchestrator.materialize_call (spec §5.3, §9).

Pins: QUEUED root creation (key, fresh room, attempt 1, dynamic_vars/profile
copies), the per-contact daily repetition cap on contact-local dates (including the
DST 25-hour fall-back day), the DNC gate (advisory phone lock first, terminal
DNC_BLOCKED row consuming the key), and the verified-ownership replay path on
idempotency-key collisions (adopt our own root via SAVEPOINT, refuse foreign
rows with an ERROR — a key never dials twice either way).
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from usan_api import schedule_orchestrator
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import AgentProfile, Call, Contact
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.settings import Settings

NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
LOCAL_DAY = date(2026, 6, 10)


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
                "TRUNCATE calls, dnc_list, agent_profile_versions, agent_profiles, contacts CASCADE"
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


async def _seed_contact(factory, *, timezone: str = "UTC") -> Contact:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="A", phone_e164=phone, timezone=timezone
        )
        await db.commit()
    return contact


async def _seed_root(
    factory, *, contact_id: uuid.UUID, key: str, scheduled_at: datetime
) -> uuid.UUID:
    """Pre-existing autonomous root (reserved-prefix key) for cap/replay scenarios."""
    async with factory() as db:
        call = Call(
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            idempotency_key=key,
            scheduled_at=scheduled_at,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id


async def _materialize(
    db: AsyncSession,
    settings: Settings,
    contact: Contact,
    *,
    key: str,
    scheduled_at: datetime = NOW,
    local_day: date = LOCAL_DAY,
    dynamic_vars: dict | None = None,
    profile_override: uuid.UUID | None = None,
) -> schedule_orchestrator.MaterializeOutcome:
    return await schedule_orchestrator.materialize_call(
        db,
        settings,
        contact=contact,
        idempotency_key=key,
        scheduled_at=scheduled_at,
        local_day=local_day,
        dynamic_vars=dynamic_vars or {},
        profile_override=profile_override,
    )


async def _count_calls(factory) -> int:
    async with factory() as db:
        result = await db.execute(select(func.count()).select_from(Call))
        return int(result.scalar_one())


async def test_materialize_creates_queued_root_with_key_and_room(session_factory):
    contact = await _seed_contact(session_factory)
    async with session_factory() as db:
        profile = AgentProfile(name=f"profile-{uuid.uuid4()}", draft_config={})
        db.add(profile)
        await db.flush()
        profile_id = profile.id
        await db.commit()
    key = f"sched:{uuid.uuid4()}:2026-06-10"

    async with session_factory() as db:
        outcome = await _materialize(
            db,
            _settings(),
            contact,
            key=key,
            dynamic_vars={"contact_name": "A"},
            profile_override=profile_id,
        )
        await db.commit()

    assert outcome.result == "created"
    call = outcome.call
    assert call is not None
    assert call.status is CallStatus.QUEUED
    assert call.attempt == 1
    assert call.parent_call_id is None
    assert call.scheduled_at == NOW
    assert call.idempotency_key == key
    assert call.livekit_room is not None
    assert call.livekit_room.startswith("usan-outbound-")
    assert call.dynamic_vars == {"contact_name": "A"}
    assert call.profile_override == profile_id


async def test_daily_cap_blocks_third_root_same_local_date(session_factory):
    # §9 daily-cap scenario: a schedule root + a batch root already exist on the
    # contact-local date; with the default cap of 2 the third root is skipped.
    contact = await _seed_contact(session_factory)
    await _seed_root(
        session_factory,
        contact_id=contact.id,
        key=f"sched:{uuid.uuid4()}:2026-06-10",
        scheduled_at=NOW,
    )
    await _seed_root(
        session_factory, contact_id=contact.id, key=f"batch:{uuid.uuid4()}:0", scheduled_at=NOW
    )

    async with session_factory() as db:
        outcome = await _materialize(db, _settings(), contact, key=f"batch:{uuid.uuid4()}:1")
        await db.commit()

    assert outcome.result == "skipped_daily_cap"
    assert outcome.call is None
    assert await _count_calls(session_factory) == 2  # no third row


async def test_daily_cap_counts_contact_local_date_not_utc(session_factory):
    # Pacific/Auckland is UTC+12 in June: a root at 11:30Z June 10 is June 10
    # 23:30 LOCAL, while the candidate at 13:00Z June 10 is June 11 LOCAL —
    # different contact-local dates, so with cap=1 they must NOT cap together
    # (day_bounds_utc; a naive UTC-date count would block the candidate).
    contact = await _seed_contact(session_factory, timezone="Pacific/Auckland")
    await _seed_root(
        session_factory,
        contact_id=contact.id,
        key=f"sched:{uuid.uuid4()}:2026-06-10",
        scheduled_at=datetime(2026, 6, 10, 11, 30, tzinfo=UTC),
    )

    async with session_factory() as db:
        outcome = await _materialize(
            db,
            _settings(MAX_AUTONOMOUS_CALLS_PER_CONTACT_PER_DAY="1"),
            contact,
            key=f"batch:{uuid.uuid4()}:0",
            scheduled_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
            local_day=date(2026, 6, 11),
        )
        await db.commit()

    assert outcome.result == "created"
    assert outcome.call is not None


async def test_daily_cap_on_dst_fall_back_day(session_factory):
    # 2026-11-01 in America/New_York is the 25-hour fall-back day: local
    # midnight Nov 1 = 04:00Z (EDT) and local midnight Nov 2 = 05:00Z (EST).
    # Roots at 04:30Z Nov 1 AND 04:30Z Nov 2 both fall inside the local day.
    local_day = date(2026, 11, 1)
    candidate_at = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)

    capped = await _seed_contact(session_factory, timezone="America/New_York")
    await _seed_root(
        session_factory,
        contact_id=capped.id,
        key=f"sched:{uuid.uuid4()}:2026-11-01",
        scheduled_at=datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
    )
    await _seed_root(
        session_factory,
        contact_id=capped.id,
        key=f"batch:{uuid.uuid4()}:0",
        scheduled_at=datetime(2026, 11, 2, 4, 30, tzinfo=UTC),
    )
    async with session_factory() as db:
        outcome = await _materialize(
            db,
            _settings(),
            capped,
            key=f"batch:{uuid.uuid4()}:1",
            scheduled_at=candidate_at,
            local_day=local_day,
        )
        await db.commit()
    assert outcome.result == "skipped_daily_cap"

    # A root at 05:30Z Nov 2 is local Nov 2 — past the 25-hour boundary, so it
    # must NOT count toward Nov 1 (the exact spot a cached-offset bug bites).
    free = await _seed_contact(session_factory, timezone="America/New_York")
    await _seed_root(
        session_factory,
        contact_id=free.id,
        key=f"sched:{uuid.uuid4()}:2026-11-01",
        scheduled_at=datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
    )
    await _seed_root(
        session_factory,
        contact_id=free.id,
        key=f"batch:{uuid.uuid4()}:0",
        scheduled_at=datetime(2026, 11, 2, 5, 30, tzinfo=UTC),
    )
    async with session_factory() as db:
        outcome = await _materialize(
            db,
            _settings(),
            free,
            key=f"batch:{uuid.uuid4()}:1",
            scheduled_at=candidate_at,
            local_day=local_day,
        )
        await db.commit()
    assert outcome.result == "created"


async def test_dnc_blocked_creates_terminal_row_consuming_key(session_factory, monkeypatch):
    contact = await _seed_contact(session_factory)
    async with session_factory() as db:
        await dnc_repo.add_entry(db, contact.phone_e164, "asked to stop")
        await db.commit()

    locked: list[str] = []
    original_lock = dnc_repo.lock_phone

    async def _spy(db: AsyncSession, phone_e164: str) -> None:
        locked.append(phone_e164)
        await original_lock(db, phone_e164)

    monkeypatch.setattr(dnc_repo, "lock_phone", _spy)
    key = f"batch:{uuid.uuid4()}:0"

    async with session_factory() as db:
        outcome = await _materialize(db, _settings(), contact, key=key)
        await db.commit()

    assert outcome.result == "dnc_blocked"
    call = outcome.call
    assert call is not None
    assert call.status is CallStatus.DNC_BLOCKED
    assert call.idempotency_key == key  # the deterministic key is consumed
    assert locked == [contact.phone_e164]  # the advisory lock is taken first


async def test_replay_adopts_owned_existing_row(session_factory):
    # §9 deterministic-key double-materialization race: the key already exists
    # for the SAME contact (re-poll after a partial crash) -> adopt, exactly one row.
    contact = await _seed_contact(session_factory)
    key = f"sched:{uuid.uuid4()}:2026-06-10"
    existing_id = await _seed_root(
        session_factory, contact_id=contact.id, key=key, scheduled_at=NOW
    )

    async with session_factory() as db:
        outcome = await _materialize(db, _settings(), contact, key=key)
        # SAVEPOINT path — no transaction poisoning: a subsequent flush in the
        # same session must succeed after the swallowed IntegrityError.
        marker = Call(
            contact_id=contact.id, direction=CallDirection.OUTBOUND, status=CallStatus.QUEUED
        )
        db.add(marker)
        await db.flush()
        await db.commit()

    assert outcome.result == "replayed"
    assert outcome.call is not None
    assert outcome.call.id == existing_id
    async with session_factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.idempotency_key == key)
        )
        assert int(result.scalar_one()) == 1  # exactly one row carries the key


async def test_replay_refuses_foreign_row_key_conflict(session_factory):
    # A squatted key (different contact) can never substitute a wellness call
    # (§5.3 step 5): refuse to adopt, ERROR log, the foreign row untouched.
    owner = await _seed_contact(session_factory)
    foreign = await _seed_contact(session_factory)
    key = f"sched:{uuid.uuid4()}:2026-06-10"
    foreign_id = await _seed_root(session_factory, contact_id=foreign.id, key=key, scheduled_at=NOW)

    errors: list[str] = []
    handler_id = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        async with session_factory() as db:
            outcome = await _materialize(db, _settings(), owner, key=key)
            await db.commit()
    finally:
        logger.remove(handler_id)

    assert outcome.result == "key_conflict"
    assert outcome.call is None  # never linked/adopted
    assert any("key conflict" in message.lower() for message in errors)
    async with session_factory() as db:
        rows = (await db.execute(select(Call).where(Call.idempotency_key == key))).scalars().all()
        assert len(rows) == 1  # a key never dials twice — and never duplicates
        assert rows[0].id == foreign_id
        assert rows[0].contact_id == foreign.id
