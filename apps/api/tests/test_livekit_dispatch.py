import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func
from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
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
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.sip.list_outbound_trunk = AsyncMock(return_value=SimpleNamespace(items=[]))
    fake.sip.create_outbound_trunk = AsyncMock(
        return_value=SimpleNamespace(sip_trunk_id="ST_created")
    )
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.fixture(autouse=True)
def _clear_trunk_cache():
    # The resolver caches the provisioned trunk ID per process; isolate tests.
    livekit_dispatch._outbound_trunk_id_cache.clear()
    yield
    livekit_dispatch._outbound_trunk_id_cache.clear()


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
            _select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
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


# --- Outbound trunk auto-provisioning (Option B) -----------------------------


def test_outbound_configured_matrix():
    # override + caller id -> configured
    assert livekit_dispatch.outbound_configured(_settings()) is True
    # no override, no SIP creds -> not configured
    assert (
        livekit_dispatch.outbound_configured(_settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None)) is False
    )
    # no override but SIP creds present -> configured (can auto-provision)
    assert (
        livekit_dispatch.outbound_configured(
            _settings(
                LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None,
                TELNYX_SIP_USERNAME="u",
                TELNYX_SIP_PASSWORD="p",
            )
        )
        is True
    )
    # no caller id -> never configured
    assert livekit_dispatch.outbound_configured(_settings(TELNYX_CALLER_ID=None)) is False


@pytest.mark.asyncio
async def test_resolve_uses_override_without_calling_api(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    tid = await livekit_dispatch.resolve_outbound_trunk_id(_settings())  # override ST_x
    assert tid == "ST_x"
    fake.sip.list_outbound_trunk.assert_not_awaited()
    fake.sip.create_outbound_trunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_reuses_existing_trunk_by_name(monkeypatch):
    fake = _fake_api()
    fake.sip.list_outbound_trunk.return_value = SimpleNamespace(
        items=[SimpleNamespace(name="usan-telnyx-outbound", sip_trunk_id="ST_existing")]
    )
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    tid = await livekit_dispatch.resolve_outbound_trunk_id(s)
    assert tid == "ST_existing"
    fake.sip.create_outbound_trunk.assert_not_awaited()  # reused, not created


@pytest.mark.asyncio
async def test_resolve_creates_trunk_when_absent(monkeypatch):
    fake = _fake_api()  # list -> empty, create -> ST_created
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None,
        TELNYX_SIP_USERNAME="txu",
        TELNYX_SIP_PASSWORD="txp",
        TELNYX_CALLER_ID="+15550001111",
    )
    tid = await livekit_dispatch.resolve_outbound_trunk_id(s)
    assert tid == "ST_created"
    fake.sip.create_outbound_trunk.assert_awaited_once()
    req = fake.sip.create_outbound_trunk.await_args.args[0]
    assert req.trunk.name == s.livekit_outbound_trunk_name
    assert req.trunk.address == "sip.telnyx.com"
    assert list(req.trunk.numbers) == ["+15550001111"]
    assert req.trunk.auth_username == "txu"
    assert req.trunk.auth_password == "txp"


@pytest.mark.asyncio
async def test_resolve_caches_after_first_provision(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    first = await livekit_dispatch.resolve_outbound_trunk_id(s)
    second = await livekit_dispatch.resolve_outbound_trunk_id(s)
    assert first == second == "ST_created"
    fake.sip.list_outbound_trunk.assert_awaited_once()  # second call served from cache
    fake.sip.create_outbound_trunk.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_raises_without_credentials(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None,
        TELNYX_SIP_USERNAME=None,
        TELNYX_SIP_PASSWORD=None,
    )
    with pytest.raises(livekit_dispatch.OutboundDispatchError):
        await livekit_dispatch.resolve_outbound_trunk_id(s)


@pytest.mark.asyncio
async def test_dispatch_agent_ok_with_sip_creds_no_override(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    call = Call(
        id=uuid.uuid4(), direction=CallDirection.OUTBOUND, livekit_room="r2", dynamic_vars={}
    )
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    await livekit_dispatch.dispatch_agent(call, settings=s)
    fake.agent_dispatch.create_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_dial_success_autoprovisions_trunk(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.return_value = MagicMock(sip_call_id="SCL_AP")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-ap")
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    await livekit_dispatch.dial_and_classify(call_id, s)

    fake.sip.create_sip_participant.assert_awaited_once()
    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.sip_trunk_id == "ST_created"  # auto-provisioned, not a static env var
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS


# --- Auto-provisioning robustness (review hardening) --------------------------


@pytest.mark.asyncio
async def test_resolve_provisioning_failure_is_sanitized(monkeypatch):
    # A LiveKit error whose text echoes the request (with the SIP password) must
    # NOT propagate — resolve raises a sanitized OutboundProvisioningError.
    secret = "SuperSecretPass987"
    fake = _fake_api()
    fake.sip.create_outbound_trunk.side_effect = RuntimeError(
        f"twirp error: trunk {{ auth_password: {secret} }}"
    )
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None,
        TELNYX_SIP_USERNAME="u",
        TELNYX_SIP_PASSWORD=secret,
    )
    with pytest.raises(livekit_dispatch.OutboundProvisioningError) as ei:
        await livekit_dispatch.resolve_outbound_trunk_id(s)
    assert secret not in str(ei.value)
    assert ei.value.__cause__ is None  # cause dropped so no traceback carries the secret
    assert livekit_dispatch._outbound_trunk_id_cache == {}  # nothing cached on failure


@pytest.mark.asyncio
async def test_dial_provisioning_failure_marks_failed_and_retries(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_outbound_trunk.side_effect = RuntimeError("livekit unavailable")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-provfail")
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    await livekit_dispatch.dial_and_classify(call_id, s)

    fake.sip.create_sip_participant.assert_not_awaited()  # never reached the dial
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "dial_error"
    assert await _count_children(session_factory, call_id) == 1  # transient -> retry


@pytest.mark.asyncio
async def test_dial_invalidates_stale_trunk_cache_on_dial_error(monkeypatch, session_factory):
    fake = _fake_api()
    # Cache a stale trunk id; the dial then fails with a non-SIP error.
    livekit_dispatch._outbound_trunk_id_cache["usan-telnyx-outbound"] = "ST_stale"
    fake.sip.create_sip_participant.side_effect = RuntimeError("sip trunk ST_stale not found")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-stale")
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    await livekit_dispatch.dial_and_classify(call_id, s)

    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.sip_trunk_id == "ST_stale"  # used the cached (stale) id
    # Cache dropped so the scheduled retry will re-provision instead of reusing it.
    assert "usan-telnyx-outbound" not in livekit_dispatch._outbound_trunk_id_cache
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.end_reason == "dial_error"
    assert await _count_children(session_factory, call_id) == 1  # retry scheduled


@pytest.mark.asyncio
async def test_dial_outbound_dispatch_error_fails_without_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    async def _raise_dispatch(_s):
        raise livekit_dispatch.OutboundDispatchError("missing creds at resolve")

    monkeypatch.setattr(livekit_dispatch, "resolve_outbound_trunk_id", _raise_dispatch)

    call_id, _ = await _seed(session_factory, room="usan-outbound-resolve-misconfig")
    s = _settings(
        LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_SIP_USERNAME="u", TELNYX_SIP_PASSWORD="p"
    )
    await livekit_dispatch.dial_and_classify(call_id, s)

    fake.sip.create_sip_participant.assert_not_awaited()
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0  # permanent -> no retry
    fake.room.delete_room.assert_awaited_once()
