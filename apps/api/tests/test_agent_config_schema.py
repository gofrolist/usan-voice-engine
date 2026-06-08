import pytest
from pydantic import ValidationError

from usan_api.schemas.agent_config import (
    DEFAULT_AGENT_CONFIG,
    AgentConfig,
    PromptsConfig,
    SpeechAdvancedConfig,
    TimingConfig,
    ToolsConfig,
)


def test_default_config_matches_current_agent_constants():
    cfg = DEFAULT_AGENT_CONFIG
    assert cfg.llm.model == "gemini-3.1-flash-lite"
    assert cfg.stt.model == "ink-whisper"
    assert cfg.timing.answer_timeout_s == 50.0
    assert cfg.timing.max_call_duration_s == 1800
    assert set(cfg.tools.enabled) == {
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "end_call",
    }
    assert cfg.prompts.greeting.startswith("Hello! This is your daily check-in")


def test_config_round_trips_through_dict():
    data = DEFAULT_AGENT_CONFIG.model_dump()
    restored = AgentConfig.model_validate(data)
    assert restored == DEFAULT_AGENT_CONFIG


def test_legacy_config_still_deserializes():
    # Forward-compat invariant: a stored snapshot that predates later (optional)
    # fields must still validate on read. A config carrying only `prompts` must fill
    # every other bundle from defaults rather than raising — otherwise old
    # agent_profile_versions rows would 500 on GET. Guards the AgentConfig invariant.
    legacy = {"prompts": DEFAULT_AGENT_CONFIG.prompts.model_dump()}
    cfg = AgentConfig.model_validate(legacy)
    assert cfg.timing.max_call_duration_s == 1800
    assert cfg.voice == DEFAULT_AGENT_CONFIG.voice
    assert set(cfg.tools.enabled) == set(DEFAULT_AGENT_CONFIG.tools.enabled)


def test_timing_rejects_cap_below_answer_timeout():
    # Cross-field guard: the cap must exceed the answer wait or the inbound
    # watchdog could fire during the greeting.
    with pytest.raises(ValidationError):
        TimingConfig(answer_timeout_s=180.0, max_call_duration_s=60)
    # A sane ordering is accepted.
    ok = TimingConfig(answer_timeout_s=50.0, max_call_duration_s=1800)
    assert ok.max_call_duration_s == 1800


def test_prompt_field_rejects_braces():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["greeting"] = "Hello {name}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_personalization_template_rejects_unknown_slot():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["inbound_personalization_template"] = "Hi {ssn}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_personalization_template_accepts_allowed_slots():
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["inbound_personalization_template"] = "Hi {elder_name}. {last_check_in_line}"
    assert PromptsConfig.model_validate(ok)


def test_tools_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        ToolsConfig(enabled=["log_wellness", "launch_missiles"])


def test_personalization_template_rejects_stray_brace():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["inbound_personalization_template"] = "{elder_name} and {"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_speech_advanced_rejects_inverted_endpointing():
    with pytest.raises(ValidationError):
        SpeechAdvancedConfig(min_endpointing_delay_s=5.0, max_endpointing_delay_s=0.1)
