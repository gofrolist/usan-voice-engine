"""_run_web: browser participant, no SIP read, no voicemail, full side-effects.

When dispatch metadata carries ``call_type == "web_call"`` the agent must:

- parse ``call_type`` from job metadata and default to ``"phone_call"`` when absent,
- skip every SIP attribute read (no sip.phoneNumber / sip.from),
- skip voicemail detection (no VoicemailWatcher, no _run_detection_window),
- wait for the browser participant generically (WebRTC; no sip.* reads),
- register the transcript flush, metrics flush, and start call recording (full side-effects ON),
- greet and run the configured check-in conversation (matches the outbound human-answered path).

Written FIRST (TDD); fails until CallMetadata.call_type + _run_web land.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.worker import CallMetadata, parse_metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def _fake_fetch(settings, *, direction, call_id=None):
    return DEFAULT_AGENT_CONFIG


def _web_metadata(call_id: str = "web-call-1") -> str:
    return json.dumps(
        {
            "session_kind": "call",
            "call_type": "web_call",
            "call_id": call_id,
            "direction": "outbound",
            "dynamic_vars": {"first_name": "Alice"},
            "resolved_vars": {"contact_name": "Alice"},
            "timezone": "US/Eastern",
        }
    )


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def test_call_metadata_parses_call_type() -> None:
    meta = parse_metadata('{"session_kind": "call", "call_type": "web_call", "call_id": "c1"}')
    assert meta.call_type == "web_call"
    assert meta.session_kind == "call"


def test_call_metadata_defaults_call_type_to_phone() -> None:
    # An existing outbound/inbound dispatch (no call_type key) stays phone_call.
    meta = parse_metadata('{"direction": "outbound", "call_id": "c1"}')
    assert meta.call_type == "phone_call"
    assert isinstance(meta, CallMetadata)


def test_call_metadata_phone_call_explicit() -> None:
    meta = parse_metadata('{"call_type": "phone_call", "call_id": "c2"}')
    assert meta.call_type == "phone_call"


def test_call_metadata_none_defaults_phone_call() -> None:
    meta = parse_metadata(None)
    assert meta.call_type == "phone_call"


# ---------------------------------------------------------------------------
# Fixture: web_call_ctx
# ---------------------------------------------------------------------------


@pytest.fixture
def web_call_ctx(monkeypatch: pytest.MonkeyPatch):
    """A JobContext whose metadata selects the web_call branch, with all side-effects spied."""
    _settings(monkeypatch)

    spies = {
        "transcript": MagicMock(),
        "metrics": MagicMock(),
        "recording": AsyncMock(),
        "greet": AsyncMock(),
        "fetch_config": AsyncMock(return_value=DEFAULT_AGENT_CONFIG),
    }
    monkeypatch.setattr(worker, "register_transcript_flush", spies["transcript"])
    monkeypatch.setattr(worker, "register_metrics_flush", spies["metrics"])
    monkeypatch.setattr(worker, "start_call_recording", spies["recording"])
    monkeypatch.setattr(worker, "greet", spies["greet"])
    monkeypatch.setattr(worker, "fetch_agent_config", spies["fetch_config"])
    # say_recording_disclosure calls session.say; mock it out so it doesn't block
    monkeypatch.setattr(worker, "say_recording_disclosure", AsyncMock())

    captured: dict = {}

    def _fake_build_session(settings, cfg=None, userdata=None):
        captured["session_cfg"] = cfg
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.generate_reply = AsyncMock()
        session.on = MagicMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda cfg=None, **kw: MagicMock())

    participant = MagicMock()
    # A browser participant carries NO sip.* attributes; the web branch must not read them.
    participant.attributes = {}

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-web-abc"
    ctx.job.metadata = _web_metadata()
    return ctx, spies, captured


# ---------------------------------------------------------------------------
# Behavioural: no SIP read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_session_never_reads_sip_attributes(web_call_ctx) -> None:
    ctx, _spies, _captured = web_call_ctx
    # A browser participant exposes NO sip.* attributes; if the branch reads them
    # it will KeyError (the participant.attributes dict is empty).
    await worker.entrypoint(ctx)
    # The participant was awaited (generic wait, no SIP read).
    ctx.wait_for_participant.assert_awaited()


# ---------------------------------------------------------------------------
# Behavioural: no voicemail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_session_does_not_run_voicemail_detection(monkeypatch, web_call_ctx) -> None:
    # _run_detection_window must NEVER be called on a web call.
    detection_window = AsyncMock()
    monkeypatch.setattr(worker, "_run_detection_window", detection_window)

    ctx, _spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)

    detection_window.assert_not_called()


@pytest.mark.asyncio
async def test_web_session_does_not_construct_voicemail_watcher(monkeypatch, web_call_ctx) -> None:
    # VoicemailWatcher must never be instantiated on the web path.
    from usan_agent import voicemail

    original_watcher = voicemail.VoicemailWatcher
    constructed: list = []

    class _SpyWatcher(original_watcher):  # type: ignore[misc]
        def __init__(self, *a, **k):
            constructed.append(True)
            super().__init__(*a, **k)

    monkeypatch.setattr(worker, "VoicemailWatcher", _SpyWatcher)

    ctx, _spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)

    assert constructed == [], "VoicemailWatcher must not be constructed for a web_call job"


# ---------------------------------------------------------------------------
# Behavioural: full side-effects ON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_session_registers_transcript_flush(web_call_ctx) -> None:
    ctx, spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)
    spies["transcript"].assert_called_once()
    # The call_id threaded through must match the dispatch metadata.
    assert spies["transcript"].call_args.args[2] == "web-call-1"


@pytest.mark.asyncio
async def test_web_session_registers_metrics_flush(web_call_ctx) -> None:
    ctx, spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)
    spies["metrics"].assert_called_once()


@pytest.mark.asyncio
async def test_web_session_starts_recording(web_call_ctx) -> None:
    ctx, spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)
    spies["recording"].assert_awaited_once()
    assert spies["recording"].await_args.args[1] == "web-call-1"


@pytest.mark.asyncio
async def test_web_session_waits_for_participant(web_call_ctx) -> None:
    ctx, _spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)
    ctx.wait_for_participant.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_session_greets(web_call_ctx) -> None:
    ctx, spies, _captured = web_call_ctx
    await worker.entrypoint(ctx)
    spies["greet"].assert_awaited_once()


# ---------------------------------------------------------------------------
# Behavioural: timeout when no browser joins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_session_times_out_when_no_browser_joins(monkeypatch) -> None:
    """A web job where the browser never connects must not hang the worker slot."""
    _settings(monkeypatch)

    session = MagicMock()
    session.start = AsyncMock()
    session.say = AsyncMock()
    session.generate_reply = AsyncMock()
    session.on = MagicMock()
    monkeypatch.setattr(worker, "build_session", lambda *a, **k: session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda *a, **k: MagicMock())
    monkeypatch.setattr(worker, "register_transcript_flush", MagicMock())
    monkeypatch.setattr(worker, "register_metrics_flush", MagicMock())
    monkeypatch.setattr(worker, "start_call_recording", AsyncMock())
    monkeypatch.setattr(worker, "say_recording_disclosure", AsyncMock())
    monkeypatch.setattr(worker, "greet", AsyncMock())

    # Config with tiny answer_timeout so the bound fires in ~50ms.
    fast_cfg = DEFAULT_AGENT_CONFIG.model_copy(
        update={"timing": DEFAULT_AGENT_CONFIG.timing.model_copy(update={"answer_timeout_s": 0.05})}
    )
    monkeypatch.setattr(worker, "fetch_agent_config", AsyncMock(return_value=fast_cfg))

    async def _never_joins(*_a, **_k):
        await asyncio.Event().wait()

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = _never_joins
    ctx.shutdown = MagicMock()
    ctx.room.name = "usan-web-nojoin"
    ctx.job.metadata = json.dumps(
        {
            "session_kind": "call",
            "call_type": "web_call",
            "call_id": "web-nojoin-1",
            "direction": "outbound",
            "dynamic_vars": {},
            "resolved_vars": {},
            "timezone": "",
        }
    )

    await asyncio.wait_for(worker.entrypoint(ctx), timeout=5)

    ctx.shutdown.assert_called_once()
    assert ctx.shutdown.call_args.kwargs.get("reason") == "no_answer_timeout"
    session.generate_reply.assert_not_called()
    worker.greet.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: phone_call + test paths unchanged
# ---------------------------------------------------------------------------


def test_phone_call_metadata_routes_unchanged() -> None:
    """Existing outbound dispatches (no call_type) still parse as phone_call."""
    meta = parse_metadata('{"call_id": "p1", "direction": "outbound", "dynamic_vars": {}}')
    assert meta.call_type == "phone_call"
    assert meta.direction == "outbound"
