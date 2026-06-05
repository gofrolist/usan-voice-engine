from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker
from usan_agent.pipeline import RECORDING_DISCLOSURE
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

    async def _greet(_s, *, include_disclosure=True):
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
    monkeypatch.setenv("GCP_PROJECT", "g")
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
        session.say = AsyncMock()
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


async def test_outbound_registers_transcript_flush(monkeypatch):
    _settings(monkeypatch)

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.on = MagicMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda: MagicMock())
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    registered = {}

    def _fake_register(ctx, session, call_id, settings):
        registered["call_id"] = call_id
        registered["ctx"] = ctx

    monkeypatch.setattr(worker, "register_transcript_flush", _fake_register)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    assert registered["call_id"] == "call-1"
    assert registered["ctx"] is ctx


async def test_outbound_invalid_call_id_shuts_down(monkeypatch):
    # A malformed call_id in dispatch metadata is a bad dispatch: drop the job before
    # it reaches any URL path or the GCS recording key — never build a session.
    _settings(monkeypatch)
    started = {"n": 0}

    def _fake_build_session(settings, userdata=None):
        started["n"] += 1
        return MagicMock()

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    rec = AsyncMock()
    monkeypatch.setattr(worker, "start_call_recording", rec)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.shutdown = MagicMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "../../evil", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    ctx.shutdown.assert_called_once()
    assert ctx.shutdown.call_args.kwargs.get("reason") == "invalid_metadata"
    assert started["n"] == 0
    rec.assert_not_awaited()


def test_caller_phone_reads_sip_attribute():
    p = MagicMock()
    p.attributes = {"sip.phoneNumber": "+15551234567"}
    assert worker._caller_phone(p) == "+15551234567"


def test_caller_phone_none_when_absent():
    p = MagicMock()
    p.attributes = {}
    assert worker._caller_phone(p) is None


async def test_inbound_known_elder_runs_check_in(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        assert phone == "+15551234567"
        assert room == "usan-inbound-x"
        return {"call_id": "inb-1", "elder_known": True, "dynamic_vars": {"elder_name": "Ada"}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)

    built = {}

    def _fake_build_inbound_agent(dynamic_vars):
        built["dynamic_vars"] = dynamic_vars
        agent = MagicMock(name="inbound_agent")
        built["agent"] = agent
        return agent

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        session.say = AsyncMock()
        captured["session"] = session
        return session

    registered = {}

    def _fake_register(ctx, session, call_id, settings):
        registered["call_id"] = call_id

    fake_build_agent = MagicMock()
    monkeypatch.setattr(worker, "build_inbound_agent", _fake_build_inbound_agent)
    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "register_transcript_flush", _fake_register)
    monkeypatch.setattr(worker, "build_agent", fake_build_agent)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+15551234567"}

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert built["dynamic_vars"] == {"elder_name": "Ada"}
    assert captured["userdata"].call_id == "inb-1"
    assert captured["userdata"].job_ctx is ctx
    captured["session"].start.assert_awaited_once()
    assert captured["session"].start.await_args.kwargs["agent"] is built["agent"]
    captured["session"].generate_reply.assert_awaited_once()
    assert registered["call_id"] == "inb-1"
    fake_build_agent.assert_not_called()  # greet-only agent NOT used for a known elder


async def test_inbound_unknown_caller_falls_back_to_greet_only(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return None  # unknown caller / lookup failed

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    fake_inbound_agent = MagicMock()
    monkeypatch.setattr(worker, "build_inbound_agent", fake_inbound_agent)

    flushes = {"n": 0}

    def _count_register(*a: object, **k: object) -> None:
        flushes["n"] += 1

    monkeypatch.setattr(worker, "register_transcript_flush", _count_register)

    participant = MagicMock()
    participant.attributes = {}  # no caller ID

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert captured["userdata"] is None  # greet-only carries no check-in state
    fake_inbound_agent.assert_not_called()
    assert flushes["n"] == 0
    worker.greet.assert_awaited_once()
    captured["session"].start.assert_awaited_once()


async def test_inbound_unknown_elder_known_false_falls_back_to_greet_only(monkeypatch):
    # The realistic unknown-caller path: the API responds with a call record but
    # elder_known=False (the number matched no elder). Must use greet-only, not the
    # check-in agent, and must NOT register a transcript flush.
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return {"call_id": "inb-9", "elder_known": False, "dynamic_vars": {}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    fake_inbound_agent = MagicMock()
    monkeypatch.setattr(worker, "build_inbound_agent", fake_inbound_agent)

    flushes = {"n": 0}

    def _count_register(*a: object, **k: object) -> None:
        flushes["n"] += 1

    monkeypatch.setattr(worker, "register_transcript_flush", _count_register)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+19998887777"}

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert captured["userdata"] is None  # greet-only despite a non-None API response
    fake_inbound_agent.assert_not_called()
    assert flushes["n"] == 0
    worker.greet.assert_awaited_once()
    captured["session"].start.assert_awaited_once()


async def test_outbound_starts_call_recording(monkeypatch):
    _settings(monkeypatch)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.on = MagicMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda: MagicMock())
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    rec = AsyncMock(return_value="EG_1")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    rec.assert_awaited_once()
    assert rec.await_args.args[1] == "call-1"

    # Consent ordering: the spoken recording disclosure must precede egress start.
    say = captured["session"].say
    say.assert_awaited()
    assert say.await_args_list[0].args[0] == RECORDING_DISCLOSURE


async def test_inbound_known_starts_call_recording(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return {"call_id": "inb-1", "elder_known": True, "dynamic_vars": {"elder_name": "Ada"}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)
    monkeypatch.setattr(worker, "build_inbound_agent", lambda dv: MagicMock())

    captured = {}

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        session.say = AsyncMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)

    rec = AsyncMock(return_value="EG_2")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+15551234567"}
    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None

    await worker.entrypoint(ctx)

    rec.assert_awaited_once()
    assert rec.await_args.args[1] == "inb-1"

    # Consent ordering: the spoken recording disclosure must precede egress start.
    say = captured["session"].say
    assert say.await_args_list[0].args[0] == RECORDING_DISCLOSURE
