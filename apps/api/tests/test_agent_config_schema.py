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


def test_short_field_rejects_stray_single_brace():
    # A lone { or } in a one-line field is a typo — still rejected.
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["greeting"] = "Hello {name}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_short_field_accepts_double_brace_tokens():
    # Phase 2: {{token}} is allowed on short fields (the agent substitutes a value).
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["greeting"] = "Hello {{first_name}}, this is your check-in."
    parsed = PromptsConfig.model_validate(ok)
    assert "{{first_name}}" in parsed.greeting


def test_personalization_template_accepts_unknown_double_brace_token():
    # Unknown {{var}} names are warned-not-blocked (design §5.1): they pass validation.
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["inbound_personalization_template"] = "Hi {{first_name}}, talk about {{weather}}."
    assert PromptsConfig.model_validate(ok)


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


def test_system_prompt_accepts_long_text_with_braces():
    # A real migrated agent prompt (~12k chars, full of {{vars}}) must save.
    # system_prompt is passed straight to the LLM (pipeline.py:113), never
    # str.format-ed, so braces are safe there.
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["system_prompt"] = ("You are Clara. Greet {{first_name}} in {{state}}.\n" * 300)[:12000]
    parsed = PromptsConfig.model_validate(cfg)
    assert "{{first_name}}" in parsed.system_prompt


def test_checkin_flow_accepts_braces():
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["checkin_flow_instructions"] = "Ask about {{med_name}} at {{time}}."
    assert PromptsConfig.model_validate(cfg)


def test_system_prompt_rejects_over_cap():
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["system_prompt"] = "x" * 24001
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(cfg)


def test_short_field_accepts_unknown_double_brace_token():
    # Unknown {{var}} on a short field is accepted (warn-don't-block).
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["voicemail_message"] = "Sorry we missed you, {{nickname}}."
    assert PromptsConfig.model_validate(ok)


def test_personalization_template_still_accepts_legacy_single_brace_slots():
    # Back-compat: old configs use single-brace {elder_name}/{last_check_in_line}.
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["inbound_personalization_template"] = "Hi {elder_name}. {last_check_in_line}"
    assert PromptsConfig.model_validate(ok)


def test_personalization_template_rejects_unknown_single_brace_slot():
    # A non-legacy single-brace slot is still a stray brace -> rejected.
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["inbound_personalization_template"] = "Hi {ssn}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_short_field_rejects_stray_brace_even_with_valid_token():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["greeting"] = "Hello {{first_name}} and {oops"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_unknown_tokens_lists_only_non_builtin_double_brace_names():
    from usan_api.schemas.agent_config import unknown_tokens

    text = "Hi {{first_name}}, the {{weather}} is {{mood_today}}. {not_a_token}"
    assert unknown_tokens(text) == ["weather", "mood_today"]


def test_unknown_tokens_dedupes_and_preserves_first_seen_order():
    from usan_api.schemas.agent_config import unknown_tokens

    text = "{{weather}} {{weather}} {{tone}}"
    assert unknown_tokens(text) == ["weather", "tone"]


def test_unknown_tokens_respects_extra_known_names():
    from usan_api.schemas.agent_config import unknown_tokens

    # A declared custom var is "known" once passed in — not reported.
    text = "Hi {{first_name}}, special offer: {{promo}}."
    assert unknown_tokens(text, known_names=frozenset({"promo"})) == []


# --- phi_tokens_in_sensitive_fields ---


def _prompts_with(**overrides: str) -> PromptsConfig:
    """Build a PromptsConfig from DEFAULT_AGENT_CONFIG with targeted field overrides."""
    data = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    data.update(overrides)
    return PromptsConfig.model_validate(data)


def test_phi_var_in_voicemail_message_returns_one_warning():
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields

    prompts = _prompts_with(voicemail_message="Sorry we missed you, {{today_meds}} note.")
    warnings = phi_tokens_in_sensitive_fields(prompts)
    assert len(warnings) == 1
    assert "{{today_meds}}" in warnings[0]
    assert "voicemail_message" in warnings[0]


def test_non_phi_var_in_voicemail_message_returns_no_warnings():
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields

    prompts = _prompts_with(voicemail_message="Hello {{first_name}}, sorry we missed you.")
    warnings = phi_tokens_in_sensitive_fields(prompts)
    assert warnings == []


def test_phi_var_in_non_sensitive_field_returns_no_warnings():
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields

    # system_prompt is NOT in SENSITIVE_PROMPT_FIELDS — PHI there is fine.
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["system_prompt"] = cfg["system_prompt"] + " Context: {{last_check_in}}"
    prompts = PromptsConfig.model_validate(cfg)
    warnings = phi_tokens_in_sensitive_fields(prompts)
    assert warnings == []


def test_phi_var_in_two_sensitive_fields_returns_two_distinct_warnings():
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields

    prompts = _prompts_with(
        greeting="Hi {{last_check_in}}, how are you?",
        voicemail_message="We noted {{last_check_in}} last time.",
    )
    warnings = phi_tokens_in_sensitive_fields(prompts)
    assert len(warnings) == 2
    fields_mentioned = [w for w in warnings if "greeting" in w or "voicemail_message" in w]
    assert len(fields_mentioned) == 2
