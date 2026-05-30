import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
from usan_api.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_x",
        "TELNYX_CALLER_ID": "+15551230000",
    }
    base.update(overrides)
    return Settings(**base)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.mark.asyncio
async def test_dispatch_invokes_agent_and_sip(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda settings: fake)

    elder = Elder(name="Ada", phone_e164="+15551234567", timezone="UTC")
    call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        livekit_room="usan-outbound-abc",
        dynamic_vars={},
    )

    await livekit_dispatch.dispatch_outbound_call(call, elder=elder, settings=_settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    fake.sip.create_sip_participant.assert_awaited_once()

    dispatch_req = fake.agent_dispatch.create_dispatch.await_args.args[0]
    assert dispatch_req.agent_name == "usan-agent"
    assert dispatch_req.room == "usan-outbound-abc"
    assert '"call_id"' in dispatch_req.metadata
    assert str(call.id) in dispatch_req.metadata

    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.sip_call_to == "+15551234567"
    assert sip_req.sip_trunk_id == "ST_x"
    assert sip_req.room_name == "usan-outbound-abc"
    # Caller ID (TELNYX_CALLER_ID) is load-bearing — assert it is wired through.
    assert sip_req.sip_number == "+15551230000"
    # Behavioral flags that distinguish a correct outbound dial.
    assert sip_req.participant_identity == "callee"
    assert sip_req.wait_until_answered is False
    assert sip_req.play_ringtone is True


@pytest.mark.asyncio
async def test_dispatch_requires_outbound_config():
    elder = Elder(name="Ada", phone_e164="+15551234567", timezone="UTC")
    call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        livekit_room="r",
        dynamic_vars={},
    )
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)

    with pytest.raises(livekit_dispatch.OutboundDispatchError):
        await livekit_dispatch.dispatch_outbound_call(call, elder=elder, settings=settings)
