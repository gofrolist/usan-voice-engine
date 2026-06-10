import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch, quiet_hours
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_x",
        "TELNYX_CALLER_ID": "+15551230000",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _pin_inside_quiet_hours(monkeypatch) -> datetime:
    """Pin the dial moment to 12:00 UTC (inside [09:00, 21:00) for UTC elders) so
    the dial-time TCPA re-check never re-queues tests aimed at later stages."""
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)
    return now


async def _seed_dialing_retry(factory, *, room, tz="UTC"):
    """A claimed retry row: status=DIALING, attempt 2."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=tz)
        call = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            attempt=2,
            livekit_room=room,
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id, phone


async def _count_children(factory, parent_id):
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_dispatch_and_dial_blocks_on_dnc(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, phone = await _seed_dialing_retry(session_factory, room="usan-outbound-dnc")
    async with session_factory() as db:
        await dnc_repo.add_entry(db, phone, "opt-out")
        await db.commit()

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.DNC_BLOCKED
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0  # DNC is terminal, no retry


@pytest.mark.asyncio
async def test_dispatch_and_dial_misconfig_fails_without_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-mc")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dispatch_and_dial(call_id, settings)

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_delegates_to_dial_when_ok(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-ok2")
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]


@pytest.mark.asyncio
async def test_dispatch_and_dial_marks_elder_missing_failed_not_silent(
    monkeypatch, session_factory
):
    """Elder deleted after claim (ON DELETE SET NULL): the row must go FAILED
    (elder_missing), not stay DIALING for reclaim_stuck_dialing to re-queue
    forever (spec §2.3(2))."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-em")
    async with session_factory() as db:
        await db.execute(update(Call).where(Call.id == call_id).values(elder_id=None))
        await db.commit()

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "elder_missing"
    assert call.ended_at is not None
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    # Elder gone => schedule_retry returns None => the chain settles here.
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_missing_room_marks_failed(monkeypatch, session_factory):
    """Same guard, livekit_room=None: FAILED(elder_missing), never silent."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(session_factory, room=None)

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "elder_missing"
    assert call.ended_at is not None
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_requeues_outside_quiet_hours(monkeypatch, session_factory):
    """A clamp is a promise about the past: gate-induced waiting can slide a claim
    past its quiet-hours window. At 23:00 elder-local the dial must NOT proceed —
    the row flips back to QUEUED with a fresh clamp (spec §2.3(1)/§6.3(3))."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=UTC)  # 23:00 EDT on 2026-06-09
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-qh", tz="America/New_York"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.QUEUED
    assert call.scheduled_at == quiet_hours.next_allowed(now, "America/New_York")
    assert call.scheduled_at == datetime(2026, 6, 10, 13, 0, tzinfo=UTC)  # 09:00 EDT
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_and_dial_inside_quiet_hours_proceeds(monkeypatch, session_factory):
    """Regression guard: inside [09:00, 21:00) elder-local the dial proceeds."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    now = datetime(2026, 6, 10, 16, 0, tzinfo=UTC)  # 12:00 EDT
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-qh-ok", tz="America/New_York"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]


@pytest.mark.asyncio
async def test_dispatch_and_dial_invalid_tz_fails_closed(monkeypatch, session_factory):
    """An invalid elder timezone fails CLOSED: FAILED(invalid_timezone), no dial,
    no retry child (schedule_retry independently refuses invalid-tz — the chain
    settles here)."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-badtz", tz="Not/AZone"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "invalid_timezone"
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0
