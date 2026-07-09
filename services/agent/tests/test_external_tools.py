"""WS-F: agent-side external (client HTTP) tools — build raw-schema tools + delegate + wiring.

The handler forwards {name, arguments} to the API proxy and relays the result; it never raises
into the session (calm fallback on error); test-mode tools touch no network; and the agent
builders attach external tools alongside the builtins.
"""

import json
from unittest.mock import AsyncMock

from livekit.agents.llm import is_raw_function_tool

from usan_agent import api_client, check_in
from usan_agent import external_tools as et
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ExternalToolSpec
from usan_agent.settings import Settings

_SPEC = ExternalToolSpec(
    name="client_schedule",
    description="Schedule a callback.",
    parameters={"type": "object", "properties": {"phone": {"type": "string"}}},
)


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="g",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="s" * 32,
    )


_TOOL_DICT = {
    "name": "client_schedule",
    "description": "d",
    "parameters": {"type": "object", "properties": {}},
}


def _cfg_with_external(tool: dict) -> AgentConfig:
    data = DEFAULT_AGENT_CONFIG.model_dump()
    data["tools"]["external_tools"] = [tool]
    return AgentConfig.model_validate(data)


# --- build ------------------------------------------------------------------


def test_build_returns_raw_tool_with_schema():
    tools = et.build_external_tools([_SPEC], call_id="c" * 32, settings=_settings())
    assert len(tools) == 1
    assert is_raw_function_tool(tools[0])
    assert tools[0].info.name == "client_schedule"
    assert tools[0].info.raw_schema["parameters"] == _SPEC.parameters


def test_build_empty_without_call_context():
    s = _settings()
    assert et.build_external_tools([_SPEC], call_id=None, settings=s) == []
    assert et.build_external_tools([_SPEC], call_id="c" * 32, settings=None) == []
    assert et.build_external_tools([], call_id="c" * 32, settings=s) == []


# --- handler behavior -------------------------------------------------------


async def test_handler_forwards_args_and_returns_json(monkeypatch):
    mock = AsyncMock(return_value={"scheduled": True})
    monkeypatch.setattr(api_client, "call_external_tool", mock)
    tool = et.build_external_tools([_SPEC], call_id="c" * 32, settings=_settings())[0]

    out = await tool._func(raw_arguments={"phone": "+15551234567"})

    assert json.loads(out) == {"scheduled": True}
    mock.assert_awaited_once()
    assert mock.await_args.kwargs["name"] == "client_schedule"
    assert mock.await_args.kwargs["arguments"] == {"phone": "+15551234567"}


async def test_handler_swallows_errors_to_fallback(monkeypatch):
    monkeypatch.setattr(
        api_client, "call_external_tool", AsyncMock(side_effect=RuntimeError("boom"))
    )
    tool = et.build_external_tools([_SPEC], call_id="c" * 32, settings=_settings())[0]
    assert await tool._func(raw_arguments={}) == et._EXTERNAL_TOOL_FALLBACK


async def test_test_tools_make_no_api_call(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(api_client, "call_external_tool", called)
    tool = et.build_external_test_tools([_SPEC])[0]
    out = await tool._func(raw_arguments={"phone": "+15551234567"})
    assert "test mode" in out.lower()
    called.assert_not_awaited()


# --- wiring into the agent builders -----------------------------------------


def test_check_in_agent_attaches_builtins_and_external():
    cfg = _cfg_with_external(_TOOL_DICT)
    agent = check_in.build_check_in_agent(cfg, call_id="c" * 32, settings=_settings())
    names = {t.info.name for t in agent.tools}
    assert "client_schedule" in names  # external tool attached
    assert "log_wellness" in names  # builtins still present
    assert "end_call" in names


def test_test_agent_uses_noop_external_tools():
    cfg = _cfg_with_external(_TOOL_DICT)
    agent = check_in.build_test_agent(cfg)
    names = {t.info.name for t in agent.tools}
    assert "client_schedule" in names
