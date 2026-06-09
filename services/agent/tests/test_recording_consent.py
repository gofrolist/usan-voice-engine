from unittest.mock import AsyncMock, MagicMock

from usan_agent import worker
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.pipeline import GREETING, RECORDING_DISCLOSURE, greet


def test_recording_disclosure_mentions_recording():
    assert "record" in RECORDING_DISCLOSURE.lower()
    assert RECORDING_DISCLOSURE.strip()


async def test_greet_speaks_disclosure_then_greeting():
    session = AsyncMock()
    await greet(session)
    spoken = [call.args[0] for call in session.say.await_args_list]
    assert spoken == [RECORDING_DISCLOSURE, GREETING]
    # The disclosure is non-interruptible so it always plays in full.
    assert session.say.await_args_list[0].kwargs.get("allow_interruptions") is False


async def test_greet_can_skip_disclosure():
    # When the caller has already spoken the disclosure to gate egress (outbound),
    # greet must not repeat it — only the greeting is spoken.
    session = AsyncMock()
    await greet(session, include_disclosure=False)
    spoken = [call.args[0] for call in session.say.await_args_list]
    assert spoken == [GREETING]


def _settings_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "c")
    monkeypatch.setenv("GCP_PROJECT", "g")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "v")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    from usan_agent.settings import get_settings

    get_settings.cache_clear()


async def test_outbound_disclosure_precedes_recording(monkeypatch):
    # The spoken consent disclosure must complete before egress starts, so no audio
    # is captured before the caller is told the call is recorded.
    _settings_env(monkeypatch)
    manager = MagicMock()

    def _fake_build_session(settings, cfg=None, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.on = MagicMock()
        manager.attach_mock(session.say, "say")
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda cfg=None, **kw: MagicMock())
    monkeypatch.setattr(worker, "fetch_agent_config", AsyncMock(return_value=DEFAULT_AGENT_CONFIG))
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "register_metrics_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())
    rec = AsyncMock(return_value="EG_1")
    manager.attach_mock(rec, "start_call_recording")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    names = [c[0] for c in manager.mock_calls]
    assert names.index("say") < names.index("start_call_recording")
    assert manager.mock_calls[names.index("say")].args[0] == RECORDING_DISCLOSURE


async def test_inbound_disclosure_precedes_recording(monkeypatch):
    _settings_env(monkeypatch)
    manager = MagicMock()

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return {"call_id": "inb-1", "elder_known": True, "dynamic_vars": {"elder_name": "Ada"}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)
    monkeypatch.setattr(worker, "build_inbound_agent", lambda cfg, **kw: MagicMock())

    def _fake_build_session(settings, cfg=None, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        session.say = AsyncMock()
        manager.attach_mock(session.say, "say")
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "register_metrics_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "fetch_agent_config", AsyncMock(return_value=DEFAULT_AGENT_CONFIG))
    rec = AsyncMock(return_value="EG_2")
    manager.attach_mock(rec, "start_call_recording")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+15551234567"}
    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None

    await worker.entrypoint(ctx)

    names = [c[0] for c in manager.mock_calls]
    assert names.index("say") < names.index("start_call_recording")
    assert manager.mock_calls[names.index("say")].args[0] == RECORDING_DISCLOSURE
