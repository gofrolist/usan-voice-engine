from types import SimpleNamespace

import httpx

from usan_agent import api_client, transcript
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c",
        GEMINI_API_KEY="g",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="s" * 32,
    )


def _msg(role, text, ts):
    return SimpleNamespace(type="message", role=role, text_content=text, created_at=ts)


def _call(name, arguments, ts):
    return SimpleNamespace(type="function_call", name=name, arguments=arguments, created_at=ts)


def test_history_to_segments_maps_messages_and_tools():
    items = [
        _msg("system", "You are an assistant.", 100.0),  # skipped
        _msg("assistant", "Hello!", 101.0),
        _msg("user", "I'm good", 102.0),
        _call("log_wellness", '{"mood": 4}', 103.0),
        SimpleNamespace(  # ignored
            type="function_call_output", name="log_wellness", output="ok", created_at=104.0
        ),
        _msg("assistant", "", 105.0),  # empty content skipped
    ]
    segs = transcript.history_to_segments(items)
    assert [s["role"] for s in segs] == ["assistant", "user", "tool"]
    assert segs[0]["content"] == "Hello!"
    assert segs[2]["tool_name"] == "log_wellness"
    assert segs[2]["tool_args"] == {"mood": 4}
    assert segs[2]["content"] == "log_wellness"
    # started_at is an ISO-8601 string
    assert all(isinstance(s["started_at"], str) and "T" in s["started_at"] for s in segs)


def test_history_to_segments_bad_tool_args_defaults_empty():
    segs = transcript.history_to_segments([_call("x", "not json", 1.0)])
    assert segs[0]["tool_args"] == {}


def test_history_to_segments_non_dict_tool_args_defaults_empty():
    # Valid JSON that is not an object (e.g. a JSON array) -> tool_args = {}.
    segs = transcript.history_to_segments([_call("x", "[1, 2]", 1.0)])
    assert segs[0]["tool_args"] == {}


def test_history_to_segments_empty():
    assert transcript.history_to_segments([]) == []


class _FakeClient:
    captured: dict = {}
    status = 200

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return httpx.Response(
            _FakeClient.status,
            json={"count": len(json["segments"])},
            request=httpx.Request("POST", url),
        )


async def test_flush_transcript_posts(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 200
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    segs = [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00+00:00"}]
    await api_client.flush_transcript("call-1", _settings(), segs)
    assert _FakeClient.captured["url"] == "http://api:8000/v1/tools/log_transcript"
    assert _FakeClient.captured["json"] == {"call_id": "call-1", "segments": segs}
    assert _FakeClient.captured["headers"]["Authorization"].startswith("Bearer ")


async def test_flush_transcript_is_best_effort(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 500
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    # Must NOT raise even on a 500.
    await api_client.flush_transcript(
        "call-1",
        _settings(),
        [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00+00:00"}],
    )


async def test_register_transcript_flush_registers_and_posts(monkeypatch):
    posted = {}

    async def _fake_flush(call_id, settings, segments):
        posted["call_id"] = call_id
        posted["segments"] = segments

    monkeypatch.setattr(transcript.api_client, "flush_transcript", _fake_flush)

    callbacks = []
    ctx = SimpleNamespace(add_shutdown_callback=lambda cb: callbacks.append(cb))
    session = SimpleNamespace(history=SimpleNamespace(items=[_msg("user", "hi", 1.0)]))

    transcript.register_transcript_flush(ctx, session, "call-1", _settings())
    assert len(callbacks) == 1
    await callbacks[0]()  # simulate the job shutdown firing the callback
    assert posted["call_id"] == "call-1"
    assert posted["segments"][0]["content"] == "hi"


async def test_register_transcript_flush_skips_empty(monkeypatch):
    called = {"n": 0}

    async def _fake_flush(call_id, settings, segments):
        called["n"] += 1

    monkeypatch.setattr(transcript.api_client, "flush_transcript", _fake_flush)
    callbacks = []
    ctx = SimpleNamespace(add_shutdown_callback=lambda cb: callbacks.append(cb))
    session = SimpleNamespace(history=SimpleNamespace(items=[]))  # no segments
    transcript.register_transcript_flush(ctx, session, "call-1", _settings())
    await callbacks[0]()
    assert called["n"] == 0  # nothing to flush -> no POST
