"""WS-A (agent side): the lean ExternalToolSpec projection parses and drops server fields.

The worker parses a server-validated config; it must carry only the LLM-facing fields
(name/description/parameters) and ignore any url/method/secret that leaks into the payload,
so the worker is structurally unable to learn a tool's endpoint (design 2026-07-09 §5/§6).
"""

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ExternalToolSpec, ToolsConfig

_PARAMS = {"type": "object", "properties": {"phone": {"type": "string"}}}


def test_projection_parses_llm_facing_fields():
    spec = ExternalToolSpec.model_validate({"name": "t", "description": "d", "parameters": _PARAMS})
    assert spec.name == "t"
    assert spec.parameters["properties"]["phone"]["type"] == "string"


def test_server_only_fields_are_dropped():
    # A payload that (defensively) still carried url/method/secret must not surface them:
    # pydantic ignores extras, so the worker never holds the endpoint or secret.
    spec = ExternalToolSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "parameters": _PARAMS,
            "url": "https://client.example.com/fn",
            "method": "POST",
            "caller_secret": "s3cr3t",
        }
    )
    assert not hasattr(spec, "url")
    assert "url" not in spec.model_dump()
    assert "caller_secret" not in spec.model_dump()


def test_tools_config_carries_external_tools():
    cfg = ToolsConfig.model_validate(
        {"external_tools": [{"name": "t", "description": "d", "parameters": _PARAMS}]}
    )
    assert cfg.external_tools[0].name == "t"


def test_default_and_legacy_configs_have_no_external_tools():
    assert DEFAULT_AGENT_CONFIG.tools.external_tools == []
    legacy = {"prompts": DEFAULT_AGENT_CONFIG.prompts.model_dump()}
    assert AgentConfig.model_validate(legacy).tools.external_tools == []
