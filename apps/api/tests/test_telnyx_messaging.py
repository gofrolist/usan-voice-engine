import pytest

from usan_api import telnyx_messaging
from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _settings() -> Settings:
    return Settings(
        **_BASE,
        TELNYX_MESSAGING_API_KEY="KEY123",
        TELNYX_MESSAGING_PROFILE_ID="mp1",
        TELNYX_FROM_NUMBER="+15551230000",
    )


class _Resp:
    def __init__(self, payload, *, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp, captured):
        self._resp = resp
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["headers"] = headers
        return self._resp


@pytest.mark.asyncio
async def test_send_sms_success_returns_message_id(monkeypatch):
    captured: dict = {}
    resp = _Resp({"data": {"id": "msg-abc"}})
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, captured)
    )
    mid = await telnyx_messaging.send_sms(_settings(), to_number="+15557654321", body="Hello there")
    assert mid == "msg-abc"
    assert captured["url"] == "https://api.telnyx.com/v2/messages"
    assert captured["json"] == {
        "messaging_profile_id": "mp1",
        "from": "+15551230000",
        "to": "+15557654321",
        "text": "Hello there",
    }
    assert captured["headers"]["Authorization"] == "Bearer KEY123"


@pytest.mark.asyncio
async def test_send_sms_http_error_wrapped(monkeypatch):
    import httpx

    err = httpx.HTTPStatusError("400", request=None, response=None)
    resp = _Resp({}, raise_exc=err)
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, {})
    )
    with pytest.raises(telnyx_messaging.TelnyxMessagingError):
        await telnyx_messaging.send_sms(_settings(), to_number="+1555", body="x")


@pytest.mark.asyncio
async def test_send_sms_missing_id_raises(monkeypatch):
    resp = _Resp({"data": {}})
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, {})
    )
    with pytest.raises(telnyx_messaging.TelnyxMessagingError):
        await telnyx_messaging.send_sms(_settings(), to_number="+1555", body="x")
