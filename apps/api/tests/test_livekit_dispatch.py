import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func
from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.db.models import Call as _Call
from usan_api.repositories import calls as calls_repo
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


async def _seed(factory, status=CallStatus.DIALING, room="usan-outbound-x"):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="Ada", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=room,
        )
        await db.commit()
        return call.id, phone


@pytest.mark.asyncio
async def test_dispatch_agent_requires_outbound_config():
    call = Call(
        id=uuid.uuid4(), direction=CallDirection.OUTBOUND, livekit_room="r", dynamic_vars={}
    )
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    with pytest.raises(livekit_dispatch.OutboundDispatchError):
        await livekit_dispatch.dispatch_agent(call, settings=settings)


@pytest.mark.asyncio
async def test_dispatch_agent_creates_dispatch(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        livekit_room="usan-outbound-y",
        dynamic_vars={},
    )
    await livekit_dispatch.dispatch_agent(call, settings=_settings())
    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    req = fake.agent_dispatch.create_dispatch.await_args.args[0]
    assert req.agent_name == "usan-agent"
    assert req.room == "usan-outbound-y"
    assert str(call.id) in req.metadata


@pytest.mark.asyncio
async def test_dial_success_marks_in_progress(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.return_value = MagicMock(sip_call_id="SCL_OK")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, phone = await _seed(session_factory, room="usan-outbound-ok")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    fake.sip.create_sip_participant.assert_awaited_once()
    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.wait_until_answered is True
    assert sip_req.sip_call_to == phone
    assert sip_req.sip_trunk_id == "ST_x"
    assert sip_req.sip_number == "+15551230000"
    assert sip_req.room_name == "usan-outbound-ok"
    assert sip_req.participant_identity == "callee"
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS
    assert call.sip_call_id == "SCL_OK"
    fake.room.delete_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_dial_busy_marks_busy_and_deletes_room(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.side_effect = _twirp_busy()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-busy")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.BUSY
    assert call.end_reason == "sip_busy"
    fake.room.delete_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_dial_unconfigured_marks_failed_without_dialing(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-noconf")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dial_and_classify(call_id, settings)

    fake.sip.create_sip_participant.assert_not_awaited()  # never pass None into the request
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    fake.room.delete_room.assert_awaited_once()


def _twirp_busy() -> Exception:
    exc = Exception("SIP 486 Busy Here")
    exc.metadata = {"sip_status_code": "486"}
    return exc


async def _count_children(factory, parent_id):
    async with factory() as db:
        result = await db.execute(
            _select(func.count()).select_from(_Call).where(_Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_dial_busy_schedules_retry(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.side_effect = _twirp_busy()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-busy-retry")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.BUSY
    assert await _count_children(session_factory, call_id) == 1  # busy attempt 1 -> +5min child


@pytest.mark.asyncio
async def test_dial_unconfigured_does_not_schedule_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-noconf-retry")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dial_and_classify(call_id, settings)

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0  # misconfig is permanent


@pytest.mark.asyncio
async def test_dial_crash_marks_failed_and_retries_without_clobbering(monkeypatch, session_factory):
    # Crash AFTER a successful answer must NOT overwrite IN_PROGRESS nor schedule a retry.
    fake = _fake_api()
    fake.sip.create_sip_participant.return_value = MagicMock(sip_call_id="SCL_OK")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-crash")

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    # Make the inner routine crash AFTER it has marked the call answered/in_progress.
    monkeypatch.setattr(livekit_dispatch, "_dial_and_classify", _boom)
    # First mark it in_progress to simulate "already answered, then crashed".
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_OK")
        await db.commit()

    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS  # gated mark did not clobber
    assert await _count_children(session_factory, call_id) == 0
