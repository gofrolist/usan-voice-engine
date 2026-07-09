"""WS-D: /v1/runtime/agent-config projects external_tools to LLM-facing fields only.

The projection is the Surface-3 security seam: the worker must never receive a tool's url,
method, or the caller secret. Unit-tested on the pure helper.
"""

import json

from usan_api.routers.runtime import _project_config_for_worker
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ResolvedAgentConfig

_FULL_TOOL = {
    # A non-builtin name: an external tool sharing an *enabled* builtin name is rejected by
    # ToolsConfig, and the default config enables `schedule_callback`.
    "name": "client_schedule_callback",
    "description": "Schedule a callback.",
    "url": "https://client.example.com/functions/v1/schedule-callback",
    "method": "POST",
    "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
    "timeout_s": 12.0,
    "speak_during_execution": True,
}


def _resolved_with_tool() -> ResolvedAgentConfig:
    data = DEFAULT_AGENT_CONFIG.model_dump()
    data["tools"]["external_tools"] = [_FULL_TOOL]
    cfg = AgentConfig.model_validate(data)
    return ResolvedAgentConfig(source="resolved", profile_id=None, version=1, config=cfg)


def test_projection_strips_url_method_secret_when_enabled():
    payload = _project_config_for_worker(_resolved_with_tool(), external_tools_enabled=True)
    ext = payload["config"]["tools"]["external_tools"]
    assert ext == [
        {
            "name": "client_schedule_callback",
            "description": "Schedule a callback.",
            "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
        }
    ]
    # Security seam: the endpoint URL must not appear anywhere in the worker payload.
    assert "client.example.com" not in json.dumps(payload)
    assert "timeout_s" not in ext[0]
    assert "method" not in ext[0]


def test_projection_empty_when_disabled():
    payload = _project_config_for_worker(_resolved_with_tool(), external_tools_enabled=False)
    assert payload["config"]["tools"]["external_tools"] == []
    assert "client.example.com" not in json.dumps(payload)
