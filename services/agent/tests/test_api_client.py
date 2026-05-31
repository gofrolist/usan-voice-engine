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
