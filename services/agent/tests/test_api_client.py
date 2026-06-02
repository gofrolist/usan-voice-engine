import jwt
import pytest

from usan_agent import api_client
from usan_agent.settings import Settings

SECRET = "s" * 32


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="cart",
        GEMINI_API_KEY="gem",
        DEFAULT_CARTESIA_VOICE_ID="voice",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY=SECRET,
    )


@pytest.mark.asyncio
async def test_report_voicemail_left_posts_signed_request(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    await api_client.report_voicemail_left("call-123", _settings())

    assert captured["url"] == "http://api:8000/v1/calls/call-123/outcome"
    assert captured["json"] == {"outcome": "voicemail_left"}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["call_id"] == "call-123"
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_report_voicemail_left_swallows_errors(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _BoomClient)
    # must NOT raise — the hangup proceeds regardless
    await api_client.report_voicemail_left("call-456", _settings())


@pytest.mark.asyncio
async def test_start_inbound_call_posts_worker_token_and_returns_json(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "call_id": "inb-1",
                "elder_known": True,
                "dynamic_vars": {"elder_name": "Ada"},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    result = await api_client.start_inbound_call("+15551234567", "usan-inbound-1", _settings())

    assert result == {
        "call_id": "inb-1",
        "elder_known": True,
        "dynamic_vars": {"elder_name": "Ada"},
    }
    assert captured["url"] == "http://api:8000/v1/calls/inbound"
    assert captured["json"] == {
        "phone_e164": "+15551234567",
        "livekit_room": "usan-inbound-1",
        "sip_call_id": None,
    }
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert "call_id" not in claims  # worker token is NOT call-scoped
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_start_inbound_call_returns_none_on_error(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _BoomClient)
    result = await api_client.start_inbound_call(None, "usan-inbound-2", _settings())
    assert result is None  # best-effort: worker falls back to greet-only
