"""WS-A: ExternalToolSpec structural validation + the save-time egress/collision gate.

Surface 3 (client HTTP tools, design 2026-07-09). Two layers under test:
  - STRUCTURAL checks on ``ExternalToolSpec`` / ``ToolsConfig`` (always-on, must stay
    forward-compat: a stored snapshot re-deserializes on read).
  - The config-DEPENDENT ``external_tool_violations`` save-time gate (allow-list +
    builtin-name collision), which must NOT be a pydantic validator.
"""

import pytest
from pydantic import ValidationError

from usan_api.schemas.agent_config import (
    DEFAULT_AGENT_CONFIG,
    MAX_EXTERNAL_TOOLS,
    AgentConfig,
    ExternalToolSpec,
    ToolsConfig,
    external_tool_violations,
)

_PARAMS = {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}


def _spec(**over):
    base = {
        "name": "schedule_callback_ext",
        "description": "Schedule a callback.",
        "url": "https://client.example.com/functions/v1/schedule-callback",
        "parameters": _PARAMS,
    }
    base.update(over)
    return base


# --- structural validation (forward-compat-safe, always on) ------------------


def test_external_tool_spec_defaults():
    spec = ExternalToolSpec.model_validate(_spec())
    assert spec.method == "POST"
    assert spec.timeout_s == 10.0
    assert spec.speak_during_execution is False


def test_url_must_be_https():
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(url="http://client.example.com/fn"))


def test_parameters_must_be_json_schema_object():
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(parameters={"type": "array", "items": {}}))
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(parameters={"type": "object"}))  # no properties


def test_no_arg_tool_allows_empty_properties():
    spec = ExternalToolSpec.model_validate(_spec(parameters={"type": "object", "properties": {}}))
    assert spec.parameters["properties"] == {}


def test_name_pattern_rejects_bad_chars():
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(name="bad name!"))


def test_timeout_range_enforced():
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(timeout_s=60.0))


def test_method_literal():
    assert ExternalToolSpec.model_validate(_spec(method="GET")).method == "GET"
    with pytest.raises(ValidationError):
        ExternalToolSpec.model_validate(_spec(method="DELETE"))


# --- ToolsConfig integration -------------------------------------------------


def test_tools_config_accepts_external_tools():
    cfg = ToolsConfig(external_tools=[_spec()])
    assert cfg.external_tools[0].name == "schedule_callback_ext"
    # enabled builtins untouched by the presence of external tools.
    assert "raise_crisis" in cfg.enabled


def test_duplicate_external_tool_names_rejected():
    with pytest.raises(ValidationError):
        ToolsConfig(external_tools=[_spec(name="dup"), _spec(name="dup")])


def test_external_tools_count_capped():
    too_many = [_spec(name=f"t{i}") for i in range(MAX_EXTERNAL_TOOLS + 1)]
    with pytest.raises(ValidationError):
        ToolsConfig(external_tools=too_many)


def test_external_tool_spec_may_reuse_a_builtin_name():
    # An ExternalToolSpec on its own never checks the catalog — a migrated Retell client
    # legitimately names its own edge function after one of our builtins.
    spec = ExternalToolSpec.model_validate(_spec(name="raise_crisis"))
    assert spec.name == "raise_crisis"


def test_tools_config_rejects_enabled_and_external_same_name():
    # raise_crisis is enabled by default; ALSO declaring it as an external tool is ambiguous
    # (double-registration / a safety builtin shadowed) — a structural, within-config error.
    with pytest.raises(ValidationError):
        ToolsConfig(external_tools=[_spec(name="raise_crisis")])


def test_external_tool_may_reuse_a_non_enabled_builtin_name():
    # schedule_callback is a catalog builtin, but if it is NOT enabled for this agent an
    # external tool may take that name (the migrated client's own function). This is the
    # normal shape of a compat agent: builtins off, tools external.
    cfg = ToolsConfig(enabled=["end_call"], external_tools=[_spec(name="schedule_callback")])
    assert cfg.external_tools[0].name == "schedule_callback"


# --- forward compatibility ---------------------------------------------------


def test_agent_config_with_external_tools_round_trips():
    data = DEFAULT_AGENT_CONFIG.model_dump()
    data["tools"]["external_tools"] = [_spec()]
    restored = AgentConfig.model_validate(data)
    assert restored.tools.external_tools[0].url.startswith("https://")
    assert AgentConfig.model_validate(restored.model_dump()) == restored


def test_legacy_config_without_external_tools_defaults_empty():
    legacy = {"prompts": DEFAULT_AGENT_CONFIG.prompts.model_dump()}
    cfg = AgentConfig.model_validate(legacy)
    assert cfg.tools.external_tools == []


# --- save-time gate: external_tool_violations --------------------------------

_ALLOWED = frozenset({"client.example.com"})


def _config_with(*specs):
    return {"tools": {"external_tools": list(specs)}}


def test_violations_flags_disallowed_host():
    cfg = _config_with(_spec(url="https://evil.example.net/fn"))
    v = external_tool_violations(cfg, allowed_hosts=_ALLOWED)
    assert len(v) == 1
    assert v[0]["type"] == "value_error.external_tool_host_not_allowed"
    assert v[0]["loc"] == ["body", "config", "tools", "external_tools", 0, "url"]


def test_violations_ignores_builtin_name_when_host_allowed():
    # The helper does NOT flag a catalog-name collision (that's the ToolsConfig structural
    # check) — a builtin-named tool on an allowed host produces no violation here.
    cfg = _config_with(_spec(name="raise_crisis"))
    assert external_tool_violations(cfg, allowed_hosts=_ALLOWED) == []


def test_violations_none_for_allowed_non_builtin():
    cfg = _config_with(_spec())
    assert external_tool_violations(cfg, allowed_hosts=_ALLOWED) == []


def test_violations_tolerates_absent_tools():
    assert external_tool_violations({}, allowed_hosts=_ALLOWED) == []
    assert external_tool_violations({"tools": {}}, allowed_hosts=_ALLOWED) == []
    none_tools = {"tools": {"external_tools": None}}
    assert external_tool_violations(none_tools, allowed_hosts=_ALLOWED) == []
