import jwt
import pytest

from usan_agent import api_client
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.settings import Settings

SECRET = "s" * 32


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="cart",
        GCP_PROJECT="gem",
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
async def test_post_metrics_swallows_network_errors(monkeypatch):
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
    # must NOT raise — the call hangup proceeds regardless
    await api_client.post_metrics("call-123", _settings(), {"turns": [], "usage": {}})


async def test_post_metrics_swallows_bad_call_id(monkeypatch):
    # The best-effort contract must hold even for a malformed call_id: never raise.
    called = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            called["n"] += 1
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise AssertionError("must not POST a malformed call_id")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    await api_client.post_metrics("bad id", _settings(), {"turns": [], "usage": {}})
    assert called["n"] == 0  # validation failed closed before any network call


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


@pytest.mark.parametrize(
    "call_id",
    [
        "../../v1/calls/secret",  # path traversal
        "call 1",  # space
        "call/1",  # slash
        "",  # empty
        "a" * 65,  # too long
        "call?x=1",  # query injection
    ],
)
async def test_post_tool_rejects_malformed_call_id(call_id):
    # A non-UUID-shaped call_id must be rejected before it reaches a URL path.
    with pytest.raises(ValueError, match="call_id"):
        await api_client.report_end_call(call_id, _settings(), "x")


@pytest.mark.parametrize("call_id", ["abc-123_DEF", "a", "a" * 64])
def test_validate_call_id_accepts_uuid_shape(call_id):
    assert api_client._validate_call_id(call_id) == call_id


async def test_report_voicemail_left_swallows_bad_call_id(monkeypatch):
    # The best-effort contract must hold even for a malformed call_id: never raise.
    called = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            called["n"] += 1
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise AssertionError("must not POST a malformed call_id")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    await api_client.report_voicemail_left("bad id", _settings())
    assert called["n"] == 0  # validation failed closed before any network call


@pytest.mark.asyncio
async def test_post_metrics_posts_signed_request(monkeypatch):
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

    payload = {"turns": [{"turn_index": 0}], "usage": {"llm_prompt_tokens": 5}}
    await api_client.post_metrics("call-123", _settings(), payload)

    assert captured["url"] == "http://api:8000/v1/tools/log_metrics"
    assert captured["json"]["call_id"] == "call-123"
    assert captured["json"]["turns"] == [{"turn_index": 0}]
    assert captured["json"]["usage"] == {"llm_prompt_tokens": 5}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["call_id"] == "call-123"


@pytest.mark.asyncio
async def test_fetch_agent_config_parses_config(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            doc = DEFAULT_AGENT_CONFIG.model_dump()
            doc["voice"]["cartesia_voice_id"] = "resolved-voice"
            return {"source": "resolved", "profile_id": "p", "version": 3, "config": doc}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params, headers):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound", call_id="call-1")

    assert cfg.voice.cartesia_voice_id == "resolved-voice"
    assert captured["url"] == "http://api:8000/v1/runtime/agent-config"
    assert captured["params"] == {"direction": "outbound", "call_id": "call-1"}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert "call_id" not in claims  # worker token, NOT call-scoped


@pytest.mark.asyncio
async def test_fetch_agent_config_omits_call_id_when_absent(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"source": "default", "config": DEFAULT_AGENT_CONFIG.model_dump()}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params, headers):
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="inbound")
    assert captured["params"] == {"direction": "inbound"}
    assert cfg == DEFAULT_AGENT_CONFIG


@pytest.mark.asyncio
async def test_fetch_agent_config_returns_default_on_error(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _BoomClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound", call_id="call-1")
    assert cfg == DEFAULT_AGENT_CONFIG  # never raises; local default


@pytest.mark.asyncio
async def test_fetch_agent_config_returns_default_on_bad_body(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"source": "resolved"}  # missing "config"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound")
    assert cfg == DEFAULT_AGENT_CONFIG
