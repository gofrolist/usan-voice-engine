"""Tests for agent_config schema — forward-compat and field contract."""

from usan_api.schemas.agent_config import AgentConfig, LLMConfig

# Minimal valid prompts dict — all 8 fields in PromptsConfig are required with no defaults.
_PROMPTS = {
    "system_prompt": "hi",
    "greeting": "hi",
    "recording_disclosure": "hi",
    "voicemail_message": "hi",
    "checkin_flow_instructions": "hi",
    "goodbye_message": "hi",
    "inbound_opening": "hi",
    "inbound_personalization_template": "hi",
}


def test_llm_config_knowledge_base_ids_defaults_none() -> None:
    assert LLMConfig().knowledge_base_ids is None


def test_agent_config_forward_compat_without_kb_ids() -> None:
    # An OLD published config snapshot has no knowledge_base_ids under llm — must still validate.
    cfg = AgentConfig.model_validate(
        {"prompts": _PROMPTS, "llm": {"model": "gemini-3.1-flash-lite"}}
    )
    assert cfg.llm.knowledge_base_ids is None


def test_agent_config_round_trips_kb_ids() -> None:
    cfg = AgentConfig.model_validate(
        {
            "prompts": _PROMPTS,
            "llm": {"model": "gemini-3.1-flash-lite", "knowledge_base_ids": ["knowledge_base_x"]},
        }
    )
    assert cfg.llm.knowledge_base_ids == ["knowledge_base_x"]
