"""T070 (US8): callback auto-dial poller.

The ``callback_dialer`` poll cycle claims due ``open`` callback requests (those with a
parsed ``requested_at`` in the past), clamps the dial time to the elder's quiet hours,
honors the DNC list, and materializes ONE outbound root Call per callback with a
deterministic ``callback:{id}`` idempotency key (FR-030/031). The callback row advances
``open -> scheduled`` (a Call now exists) and a later reconcile pass advances
``scheduled -> dialed`` once that Call has left the queue. Re-running the cycle never
double-dials. Mirrors the scheduler poller's seed/poll_once/assert harness.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import callback_dialer
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallbackRequest
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings

# 2026-06-15 15:00Z — inside the statutory [09:00, 21:00) window in UTC.
NOW = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE callback_requests, calls, dnc_list, elders CASCADE"))
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


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


async def _seed(
    session_factory,
    *,
    tz: str = "UTC",
    requested_at: datetime | None,
    status: str = "open",
    on_dnc: bool = False,
    profile_override: uuid.UUID | None = None,
    call_status: CallStatus = CallStatus.COMPLETED,
) -> tuple[uuid.UUID, int]:
    """Create elder + a source call + a callback request; return (elder_id, callback_id)."""
    async with session_factory() as db:
        elder = await elders_repo.create_elder(db, name="Ada", phone_e164=_phone(), timezone=tz)
        src = await calls_repo.create_call(
            db, elder_id=elder.id, direction=CallDirection.OUTBOUND, status=call_status
        )
        if on_dnc:
            await dnc_repo.add_entry(db, elder.phone_e164, "test opt-out")
        cb = await callback_requests_repo.create_callback_request(
            db,
            call_id=src.id,
            elder_id=elder.id,
            requested_time_text="call me back",
            requested_at=requested_at,
            notes=None,
            profile_override=profile_override,
        )
        if status != "open":
            await db.execute(
                update(CallbackRequest)
                .where(CallbackRequest.id == cb.id)
                .values(status=status, dispatched_call_id=src.id)
            )
        await db.commit()
        return elder.id, cb.id


async def _callback(session_factory, cb_id: int) -> CallbackRequest:
    async with session_factory() as db:
        return (
            await db.execute(select(CallbackRequest).where(CallbackRequest.id == cb_id))
        ).scalar_one()


async def _calls_for(session_factory, elder_id: uuid.UUID) -> list[Call]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Call).where(Call.elder_id == elder_id, Call.parent_call_id.is_(None))
            )
        ).scalars()
        return list(rows)


async def test_due_open_callback_materializes_queued_call(session_factory):
    elder_id, cb_id = await _seed(
        session_factory, tz="UTC", requested_at=NOW - timedelta(minutes=5)
    )

    counts = await callback_dialer.poll_once(session_factory, _settings(), now=NOW)
    assert counts["materialized"] == 1

    cb = await _callback(session_factory, cb_id)
    assert cb.status == "scheduled"
    assert cb.dispatched_call_id is not None

    dialed = await _calls_for(session_factory, elder_id)
    queued = [c for c in dialed if c.status is CallStatus.QUEUED]
    assert len(queued) == 1
    call = queued[0]
    assert call.idempotency_key == f"callback:{cb_id}"
    assert call.id == cb.dispatched_call_id
    # In-window: scheduled at the current moment (max(requested_at, now)), not the past.
    assert call.scheduled_at == NOW


async def test_quiet_hours_deferral(session_factory):
    # 07:00Z == 03:00 EDT — inside quiet hours; the dial defers to 09:00 EDT (13:00Z).
    req = datetime(2026, 6, 15, 7, 0, tzinfo=UTC)
    elder_id, cb_id = await _seed(session_factory, tz="America/New_York", requested_at=req)

    await callback_dialer.poll_once(session_factory, _settings(), now=req)

    call = [
        c for c in await _calls_for(session_factory, elder_id) if c.status is CallStatus.QUEUED
    ][0]
    local = call.scheduled_at.astimezone(ZoneInfo("America/New_York"))
    assert local.hour == 9
    assert local.minute == 0


async def test_dnc_blocks_callback(session_factory):
    elder_id, cb_id = await _seed(
        session_factory, tz="UTC", requested_at=NOW - timedelta(minutes=5), on_dnc=True
    )

    await callback_dialer.poll_once(session_factory, _settings(), now=NOW)

    blocked = [
        c for c in await _calls_for(session_factory, elder_id) if c.status is CallStatus.DNC_BLOCKED
    ]
    assert len(blocked) == 1
    assert blocked[0].idempotency_key == f"callback:{cb_id}"
    assert blocked[0].scheduled_at is None

    cb = await _callback(session_factory, cb_id)
    # The materialized call is terminal at birth (DNC_BLOCKED, not QUEUED), so the same
    # cycle's reconcile pass advances the callback straight to 'dialed'.
    assert cb.status == "dialed"
    assert cb.dispatched_call_id == blocked[0].id


async def test_idempotent_no_duplicate_call(session_factory):
    elder_id, cb_id = await _seed(
        session_factory, tz="UTC", requested_at=NOW - timedelta(minutes=5)
    )

    await callback_dialer.poll_once(session_factory, _settings(), now=NOW)
    await callback_dialer.poll_once(session_factory, _settings(), now=NOW)

    roots = [
        c
        for c in await _calls_for(session_factory, elder_id)
        if c.idempotency_key == f"callback:{cb_id}"
    ]
    assert len(roots) == 1  # second cycle does not re-materialize


async def test_future_callback_not_due(session_factory):
    elder_id, cb_id = await _seed(session_factory, tz="UTC", requested_at=NOW + timedelta(hours=2))

    counts = await callback_dialer.poll_once(session_factory, _settings(), now=NOW)
    assert counts["materialized"] == 0
    assert (await _callback(session_factory, cb_id)).status == "open"


async def test_null_requested_at_not_dialed(session_factory):
    # A callback the LLM could not resolve to a time stays in the ops queue (open),
    # never auto-dialed.
    elder_id, cb_id = await _seed(session_factory, tz="UTC", requested_at=None)

    counts = await callback_dialer.poll_once(session_factory, _settings(), now=NOW)
    assert counts["materialized"] == 0
    assert (await _callback(session_factory, cb_id)).status == "open"


async def test_reconcile_scheduled_to_dialed(session_factory):
    # A 'scheduled' callback whose dispatched call has left the queue (DIALING) is
    # reconciled to 'dialed'.
    elder_id, cb_id = await _seed(
        session_factory,
        tz="UTC",
        requested_at=NOW - timedelta(minutes=5),
        status="scheduled",
        call_status=CallStatus.DIALING,
    )

    counts = await callback_dialer.poll_once(session_factory, _settings(), now=NOW)
    assert counts["dialed"] == 1
    assert (await _callback(session_factory, cb_id)).status == "dialed"


async def _create_profile(session_factory) -> uuid.UUID:
    async with session_factory() as db:
        profile = await profiles_repo.create_profile(
            db, name=f"es-{uuid.uuid4().hex}", description=None, actor_email="t@usan.test"
        )
        await db.commit()
        return profile.id


async def test_profile_override_propagates_to_materialized_call(session_factory):
    # End-to-end (SC-011): a Spanish callback's profile_override flows through the dialer
    # into the materialized outbound call, so the callback is dialed with the Spanish profile.
    profile_id = await _create_profile(session_factory)
    elder_id, _cb_id = await _seed(
        session_factory,
        tz="UTC",
        requested_at=NOW - timedelta(minutes=5),
        profile_override=profile_id,
    )

    await callback_dialer.poll_once(session_factory, _settings(), now=NOW)

    call = [
        c for c in await _calls_for(session_factory, elder_id) if c.status is CallStatus.QUEUED
    ][0]
    assert call.profile_override == profile_id
