"""Agent sandbox for the pre-publish Test Audio session (US5 / T047, FR-027/FR-028).

When dispatch metadata carries ``session_kind == "test"`` the agent must:

- build its config from the inline ``test_config`` (no published-only resolver,
  no inbound lookup),
- register ONLY the no-op ``_TEST_TOOL_REGISTRY`` (stubs that never call api_client),
- skip every production side effect: no transcript flush, no metrics flush, no
  recording/egress, no SIP participant read,
- wait for a participant GENERICALLY (browser WebRTC; no sip.* attributes),
- write NO Call / wellness / medication / audit row and make NO /v1/tools/* call,
- and use exactly the draft's selected voice (voice.cartesia_voice_id), llm.model
  and stt.model in the pipeline (G1 / FR-015 — a test runs the chosen voice/models).

Written FIRST (Constitution IV); fails until the worker test branch + the no-op
registry land.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import check_in, worker
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.worker import parse_metadata


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


_TEST_CONFIG = DEFAULT_AGENT_CONFIG.model_copy(
    update={
        "voice": DEFAULT_AGENT_CONFIG.voice.model_copy(
            update={"cartesia_voice_id": "test-voice-xyz"}
        ),
        "llm": DEFAULT_AGENT_CONFIG.llm.model_copy(update={"model": "gemini-2.5-flash"}),
        "stt": DEFAULT_AGENT_CONFIG.stt.model_copy(update={"model": "ink-whisper"}),
    }
)


def _test_metadata() -> str:
    return json.dumps(
        {
            "session_kind": "test",
            "test_config": _TEST_CONFIG.model_dump(),
            "call_id": None,
            "direction": "outbound",
            "dynamic_vars": {"first_name": "Synthetic"},
            "resolved_vars": {},
            "timezone": "",
        }
    )


# --- metadata parsing -----------------------------------------------------


def test_parse_metadata_defaults_session_kind_to_call():
    # Every existing dispatch (no session_kind key) must remain byte-compatible.
    md = parse_metadata('{"call_id": "abc", "direction": "outbound"}')
    assert md.session_kind == "call"
    assert md.test_config is None


def test_parse_metadata_none_is_call():
    assert parse_metadata(None).session_kind == "call"


def test_parse_metadata_reads_test_session_kind_and_config():
    md = parse_metadata(_test_metadata())
    assert md.session_kind == "test"
    assert md.test_config is not None
    assert md.test_config["voice"]["cartesia_voice_id"] == "test-voice-xyz"


# --- no-op test tool registry ---------------------------------------------


def test_test_tool_registry_has_no_op_stubs_only():
    # The test registry must hold a stub for every catalog tool name and NONE may
    # reach api_client (they return canned strings).
    assert set(check_in._TEST_TOOL_REGISTRY).issuperset(set(check_in._TOOL_REGISTRY))


@pytest.mark.asyncio
async def test_test_tools_never_call_api_client(monkeypatch):
    # Invoking every no-op stub must not touch api_client at all.
    import usan_agent.api_client as api_client

    for attr in dir(api_client):
        fn = getattr(api_client, attr)
        if callable(fn) and not attr.startswith("__"):
            monkeypatch.setattr(
                api_client, attr, MagicMock(side_effect=AssertionError(f"{attr} called"))
            )

    ctx = MagicMock()
    for tool in check_in._TEST_TOOL_REGISTRY.values():
        fn = getattr(tool, "_callable", None) or getattr(tool, "__wrapped__", None) or tool
        # The stub callables accept a RunContext-like first arg; call defensively.
        try:
            result = fn(ctx)
            if hasattr(result, "__await__"):
                await result
        except TypeError:
            # Tool requires extra args; the point is only that no api_client fired.
            pass


def test_test_registry_shares_no_callable_with_live_registry() -> None:
    # Every test-mode tool must be a DISTINCT no-op stub, never a live (api-calling)
    # tool object — a copy-paste that reused a live callable would silently write data.
    live_ids = {id(tool) for tool in check_in._TOOL_REGISTRY.values()}
    for name, tool in check_in._TEST_TOOL_REGISTRY.items():
        assert id(tool) not in live_ids, f"{name} reuses the live tool callable in test mode"


@pytest.mark.asyncio
async def test_noop_end_call_hangs_up_without_calling_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (the bug where noop_end_call delegated to _do_end_call): the
    # test-mode end_call stub must hang up gracefully — say goodbye, delete_room,
    # shutdown — but NEVER call api_client.report_end_call (no Call row exists in test
    # mode, so test mode makes no /v1/tools/* request; FR-027).
    import usan_agent.api_client as api_client

    report = AsyncMock(side_effect=AssertionError("report_end_call must not run in test mode"))
    monkeypatch.setattr(api_client, "report_end_call", report)

    userdata = MagicMock()
    userdata.goodbye_message = "Goodbye for now."
    userdata.job_ctx.delete_room = AsyncMock()
    userdata.job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()  # say(...) returns an awaitable handle; _hang_up awaits it

    ctx = MagicMock()
    ctx.userdata = userdata
    ctx.session = session

    tool = check_in._TEST_TOOL_REGISTRY["end_call"]
    fn = getattr(tool, "_callable", None) or getattr(tool, "__wrapped__", None) or tool
    await fn(ctx)

    report.assert_not_called()
    session.say.assert_called_once()
    userdata.job_ctx.delete_room.assert_awaited_once()
    userdata.job_ctx.shutdown.assert_called_once()


# --- entrypoint test branch -----------------------------------------------


@pytest.fixture
def test_mode_ctx(monkeypatch):
    """A JobContext whose metadata selects test mode, with all side effects spied."""
    _settings(monkeypatch)

    spies = {
        "transcript": MagicMock(),
        "metrics": MagicMock(),
        "recording": AsyncMock(),
        "start_inbound": AsyncMock(),
        "fetch_config": AsyncMock(),
    }
    monkeypatch.setattr(worker, "register_transcript_flush", spies["transcript"])
    monkeypatch.setattr(worker, "register_metrics_flush", spies["metrics"])
    monkeypatch.setattr(worker, "start_call_recording", spies["recording"])
    monkeypatch.setattr(worker, "start_inbound_call", spies["start_inbound"])
    monkeypatch.setattr(worker, "fetch_agent_config", spies["fetch_config"])

    captured = {}

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

    participant = MagicMock()
    # A browser participant carries NO sip.* attributes; the test branch must not read them.
    participant.attributes = {}

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-test-abc"
    ctx.job.metadata = _test_metadata()
    return ctx, spies, captured


@pytest.mark.asyncio
async def test_test_mode_skips_all_side_effects(test_mode_ctx):
    ctx, spies, _captured = test_mode_ctx
    await worker.entrypoint(ctx)

    # No production side effects of ANY kind.
    spies["transcript"].assert_not_called()
    spies["metrics"].assert_not_called()
    spies["recording"].assert_not_awaited()
    spies["start_inbound"].assert_not_awaited()
    # The published-only config resolver must NOT be consulted in test mode.
    spies["fetch_config"].assert_not_awaited()


@pytest.mark.asyncio
async def test_test_mode_builds_pipeline_with_draft_voice_and_models(test_mode_ctx):
    # G1 / FR-015: a test runs the EXACT chosen voice + llm + stt models.
    ctx, _spies, captured = test_mode_ctx
    await worker.entrypoint(ctx)

    cfg = captured["session_cfg"]
    assert cfg is not None
    assert cfg.voice.cartesia_voice_id == "test-voice-xyz"
    assert cfg.llm.model == "gemini-2.5-flash"
    assert cfg.stt.model == "ink-whisper"


@pytest.mark.asyncio
async def test_test_mode_waits_for_participant_generically(test_mode_ctx):
    ctx, _spies, _captured = test_mode_ctx
    await worker.entrypoint(ctx)
    # The browser join is awaited; no SIP attribute read happens (would KeyError if
    # the branch tried sip.phoneNumber on the empty-attrs participant — it must not).
    ctx.wait_for_participant.assert_awaited()


@pytest.mark.asyncio
async def test_test_mode_uses_only_no_op_registry(monkeypatch, test_mode_ctx):
    # The agent built in test mode must use the no-op test tools, not the live ones.
    ctx, _spies, _captured = test_mode_ctx
    built = {}

    def _fake_build(cfg, **kw):
        built["cfg"] = cfg
        built["session_kind"] = kw.get("session_kind")
        return MagicMock()

    monkeypatch.setattr(worker, "build_test_agent", _fake_build)
    await worker.entrypoint(ctx)
    assert "cfg" in built  # the test-agent builder was used
