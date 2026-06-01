from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker
from usan_agent.voicemail import VoicemailWatcher
from usan_agent.worker import CallMetadata, parse_metadata


def test_parse_metadata_outbound():
    raw = '{"call_id": "abc", "direction": "outbound", "dynamic_vars": {"name": "Ada"}}'
    md = parse_metadata(raw)
    assert md == CallMetadata(call_id="abc", direction="outbound", dynamic_vars={"name": "Ada"})


def test_parse_metadata_none_is_inbound():
    md = parse_metadata(None)
    assert md.call_id is None
    assert md.direction == "inbound"
    assert md.dynamic_vars == {}


def test_parse_metadata_empty_string_is_inbound():
    md = parse_metadata("")
    assert md.direction == "inbound"
    assert md.call_id is None


def test_parse_metadata_invalid_json_is_inbound():
    md = parse_metadata("not json")
    assert md.direction == "inbound"
    assert md.call_id is None
    assert md.dynamic_vars == {}


@pytest.mark.asyncio
async def test_run_detection_window_triggers_voicemail(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)

    watcher = VoicemailWatcher()
    watcher.feed("you've reached the Smiths, leave a message")  # already detected

    session = MagicMock()
    ctx = MagicMock()
    greeted = []

    async def _greet(_s):
        greeted.append(True)

    monkeypatch.setattr(worker, "greet", _greet)

    await worker._run_detection_window(ctx, session, watcher, call_id="c1", settings=MagicMock())

    assert greeted == [True]
    assert left == ["c1"]


@pytest.mark.asyncio
async def test_run_detection_window_human_falls_through(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    # shorten the window so the test is fast
    monkeypatch.setattr(worker, "VOICEMAIL_WINDOW_S", 0.05)

    watcher = VoicemailWatcher()  # never fed a voicemail phrase

    await worker._run_detection_window(
        MagicMock(), MagicMock(), watcher, call_id="c2", settings=MagicMock()
    )

    assert left == []  # human → no voicemail action


def _settings(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "c")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "v")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    from usan_agent.settings import get_settings

    get_settings.cache_clear()


async def test_outbound_starts_check_in_agent(monkeypatch):
    _settings(monkeypatch)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        session.on = MagicMock()
        captured["session"] = session
        return session

    built = {}

    def _fake_build_check_in_agent():
        agent = MagicMock(name="check_in_agent")
        built["agent"] = agent
        return agent

    fake_build_agent = MagicMock()
    monkeypatch.setattr(worker, "build_agent", fake_build_agent)
    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", _fake_build_check_in_agent)
    # Short-circuit the detection window so the test doesn't run the real conversation.
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    # The session was started with a CheckInData userdata scoped to this call.
    data = captured["userdata"]
    assert data is not None
    assert data.call_id == "call-1"
    assert data.job_ctx is ctx
    # The check-in agent (not the greet-only agent) was started.
    captured["session"].start.assert_awaited_once()
    assert captured["session"].start.await_args.kwargs["agent"] is built["agent"]
    # The greet-only build_agent must NOT have been called on the outbound path.
    fake_build_agent.assert_not_called()


async def test_inbound_uses_greet_only_agent(monkeypatch):
    _settings(monkeypatch)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        captured["session"] = session
        return session

    fake = MagicMock()
    monkeypatch.setattr(worker, "build_check_in_agent", fake)
    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert captured["userdata"] is None  # inbound carries no check-in state
    # The check-in agent must NOT have been built on the inbound path.
    fake.assert_not_called()
    # The session must have been started.
    captured["session"].start.assert_awaited_once()
