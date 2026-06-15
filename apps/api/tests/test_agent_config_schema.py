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
    assert cfg.tools.enabled == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "raise_crisis",
        "schedule_callback",
        "close_family_task",
        "record_personal_fact",
        "record_survey",
        "get_activity",
        "send_sms",
        "send_info_sms",
        "register_opt_out",
        "set_spanish_callback",
        "end_call",
    ]
    assert cfg.prompts.greeting.startswith("Hello! This is your daily check-in")


def test_us7_safety_guidance_in_default_prompts():
    # T069 / FR-036: anti-scam guidance plus the opt-out and informational-SMS tools are
    # wired into BOTH the outbound check-in and the inbound personalization prompts.
    prompts = DEFAULT_AGENT_CONFIG.prompts
    for block in (prompts.checkin_flow_instructions, prompts.inbound_personalization_template):
        assert "SCAM AWARENESS" in block
        assert "register_opt_out" in block
        assert "send_info_sms" in block


def test_us8_spanish_guidance_in_default_prompts():
    # FR-040: the Spanish-callback guidance + tool are wired into BOTH default prompt blocks.
    prompts = DEFAULT_AGENT_CONFIG.prompts
    for block in (prompts.checkin_flow_instructions, prompts.inbound_personalization_template):
        assert "SPANISH" in block
        assert "set_spanish_callback" in block


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
    ok["inbound_personalization_template"] = "Hi {contact_name}. {last_check_in_line}"
    assert PromptsConfig.model_validate(ok)


def test_tools_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        ToolsConfig(enabled=["log_wellness", "launch_missiles"])


def test_tools_accepts_three_new_catalog_tools():
    # flag_for_followup / schedule_callback / send_sms are valid catalog names, so
    # ToolsConfig accepts and round-trips them. NOTE: the agent's _TOOL_REGISTRY does
    # not yet hold their callables, so enabling them saves but is a no-op agent-side
    # until Parts B/C/D land (documents the intended rollout gap, not a bug here).
    names = ["flag_for_followup", "schedule_callback", "send_sms"]
    assert ToolsConfig(enabled=names).enabled == names


def test_personalization_template_rejects_stray_brace():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["inbound_personalization_template"] = "{contact_name} and {"
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
    # Back-compat: old configs use single-brace {contact_name}/{last_check_in_line}.
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["inbound_personalization_template"] = "Hi {contact_name}. {last_check_in_line}"
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


def test_tools_config_tool_names_is_catalog_single_source():
    from usan_api.schemas.agent_config import TOOL_NAMES as CONFIG_TOOL_NAMES
    from usan_api.schemas.tool_catalog import TOOL_CATALOG, TOOL_NAMES

    assert CONFIG_TOOL_NAMES is TOOL_NAMES
    assert {t.name for t in TOOL_CATALOG} == CONFIG_TOOL_NAMES


def test_tools_accepts_all_seven_catalog_tools():
    from usan_api.schemas.tool_catalog import TOOL_CATALOG

    names = [t.name for t in TOOL_CATALOG]
    assert ToolsConfig(enabled=names).enabled == names


# --- send_sms template config (Phase 3 §6.1) -------------------------------
from usan_api.schemas.agent_config import (  # noqa: E402
    SmsTemplate,
    SmsToolConfig,
)


def test_sms_template_accepts_non_phi_tokens():
    cfg = ToolsConfig(
        enabled=list(DEFAULT_AGENT_CONFIG.tools.enabled),
        sms=SmsToolConfig(
            templates=[
                SmsTemplate(
                    key="med_reminder",
                    label="Med reminder",
                    body="Hello {{first_name}}, this is your USAN reminder for {{current_date}}.",
                )
            ]
        ),
    )
    assert cfg.sms is not None
    assert cfg.sms.templates[0].key == "med_reminder"


def test_sms_default_is_none():
    assert ToolsConfig().sms is None


@pytest.mark.parametrize(
    "token", ["last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"]
)
def test_sms_template_phi_token_hard_blocks(token):
    with pytest.raises(ValidationError) as exc:
        SmsToolConfig(
            templates=[SmsTemplate(key="bad", label="Bad", body="Your status: {{" + token + "}}")]
        )
    # the validator runs on ToolsConfig too:
    with pytest.raises(ValidationError):
        ToolsConfig(
            sms={"templates": [{"key": "bad", "label": "Bad", "body": "x {{" + token + "}}"}]}
        )
    assert (
        "protected health information" in str(exc.value).lower() or "phi" in str(exc.value).lower()
    )


def test_sms_template_key_slug_enforced():
    with pytest.raises(ValidationError):
        SmsTemplate(key="Bad Key!", label="x", body="hello")


def test_sms_config_roundtrips_through_agent_config():
    base = DEFAULT_AGENT_CONFIG.model_dump()
    base["tools"] = {
        "enabled": list(DEFAULT_AGENT_CONFIG.tools.enabled),
        "sms": {"templates": [{"key": "a", "label": "A", "body": "Hi {{first_name}}"}]},
    }
    cfg = AgentConfig.model_validate(base)
    assert cfg.tools.sms is not None
    assert cfg.tools.sms.templates[0].key == "a"


# --- phi_names generalization (custom PHI variables, spec §3.2 / plan C5) ----


def test_phi_tokens_in_sensitive_fields_accepts_custom_phi_names():
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields
    from usan_api.schemas.variable_catalog import PHI_BUILTIN_NAMES

    prompts = _prompts_with(voicemail_message="Sorry we missed you. Re: {{diagnosis}}.")
    warnings = phi_tokens_in_sensitive_fields(prompts, phi_names=PHI_BUILTIN_NAMES | {"diagnosis"})
    assert len(warnings) == 1
    # Existing message shape: token + quoted field name + the advisory sentence.
    assert "{{diagnosis}}" in warnings[0]
    assert "'voicemail_message'" in warnings[0]
    assert "protected health information" in warnings[0]


def test_phi_tokens_default_unchanged():
    # Zero-diff pin: calling with no kwarg reproduces today's builtin-only output
    # on the same prompts — a custom name is never flagged by default.
    from usan_api.schemas.agent_config import phi_tokens_in_sensitive_fields

    prompts = _prompts_with(
        voicemail_message="We noted {{last_check_in}} and {{diagnosis}} last time."
    )
    warnings = phi_tokens_in_sensitive_fields(prompts)
    assert len(warnings) == 1
    assert "{{last_check_in}}" in warnings[0]
    assert all("{{diagnosis}}" not in w for w in warnings)


# --- custom_phi_sms_violations (custom PHI in SMS bodies, spec §3.2.1 / plan C6) ----


def _config_with_sms_bodies(*bodies: str) -> dict:
    base = DEFAULT_AGENT_CONFIG.model_dump()
    base["tools"]["sms"] = {
        "templates": [
            {"key": f"t{i}", "label": f"T{i}", "body": body} for i, body in enumerate(bodies)
        ]
    }
    return base


def test_custom_phi_sms_violations_exact_loc():
    from usan_api.schemas.agent_config import custom_phi_sms_violations

    config = _config_with_sms_bodies("Hi {{first_name}}, see you soon.", "{{diagnosis}}")
    violations = custom_phi_sms_violations(config, frozenset({"diagnosis"}))
    assert len(violations) == 1
    # The fabricated field-level loc is load-bearing (spec §3.2.1): the client
    # maps it onto tools.sms.templates.1.body.
    assert violations[0]["loc"] == ["body", "config", "tools", "sms", "templates", 1, "body"]
    assert "{{diagnosis}}" in violations[0]["msg"]
    assert violations[0]["type"] == "value_error.custom_phi_sms"


def test_custom_phi_sms_violations_clean_and_absent_tools():
    from usan_api.schemas.agent_config import custom_phi_sms_violations

    phi = frozenset({"diagnosis"})
    # Clean templates -> no violations.
    clean = _config_with_sms_bodies("Hi {{first_name}}, this is USAN.")
    assert custom_phi_sms_violations(clean, phi) == []
    # tools.sms absent (None) and tools absent entirely -> tolerated, [].
    base = DEFAULT_AGENT_CONFIG.model_dump()
    assert base["tools"]["sms"] is None
    assert custom_phi_sms_violations(base, phi) == []
    no_tools = {k: v for k, v in base.items() if k != "tools"}
    assert custom_phi_sms_violations(no_tools, phi) == []
    # Builtin PHI names are NOT this helper's job (the pydantic validators block
    # them earlier): with an empty phi_names set over a builtin-PHI-free body,
    # nothing is flagged — the helper only knows the names it is handed.
    tokens = _config_with_sms_bodies("About {{diagnosis}} and {{pet_name}}.")
    assert custom_phi_sms_violations(tokens, frozenset()) == []


# --- PolicyConfig / RetryMaxAttempts (per-profile policy, spec §3.3.1 / plan D1) ----


@pytest.mark.parametrize(
    ("start", "end", "valid"),
    [
        ("08:59", None, False),  # widens past the statutory 09:00 start
        (None, "21:01", False),  # widens past the statutory 21:00 end
        ("12:00", "10:00", False),  # start >= end
        ("12:00", "12:00", False),  # start == end
        ("09:30", None, True),  # one-sided start narrowing
        (None, "20:00", True),  # one-sided end narrowing
        ("10:15", "18:45", True),  # both sides, minute granularity
        # Exact statutory boundaries: >= 09:00 and <= 21:00 are INCLUSIVE bounds,
        # so restating a statutory edge is a valid (no-op) narrowing...
        ("09:00", None, True),  # start exactly at the statutory start
        (None, "21:00", True),  # end exactly at the statutory end
        ("09:00", "21:00", True),  # the full statutory window restated
        # ...but start at the END boundary leaves an empty effective window
        # (start 21:00 >= effective end 21:00) — rejected by start < end.
        ("21:00", None, False),
        ("21:00", "21:00", False),  # equal start/end at the boundary
    ],
)
def test_policy_config_narrowing_only(start, end, valid):
    from usan_api.schemas.agent_config import PolicyConfig

    if valid:
        cfg = PolicyConfig(quiet_hours_start_local=start, quiet_hours_end_local=end)
        assert cfg.quiet_hours_start_local == start
        assert cfg.quiet_hours_end_local == end
    else:
        with pytest.raises(ValidationError):
            PolicyConfig(quiet_hours_start_local=start, quiet_hours_end_local=end)


@pytest.mark.parametrize("value", ["9:00", "09:60", "24:00", "0900", "09:00:00"])
def test_policy_config_hhmm_format(value):
    from usan_api.schemas.agent_config import PolicyConfig

    with pytest.raises(ValidationError):
        PolicyConfig(quiet_hours_start_local=value)
    with pytest.raises(ValidationError):
        PolicyConfig(quiet_hours_end_local=value)


def test_policy_time_fields_stay_strings():
    # JSONB + zod round-trip contract (spec §3.3.1): times stay "HH:MM" strings.
    # model_dump() (python mode, the save path) would TypeError on datetime.time
    # at the JSONB write, and mode="json" would round-trip "09:30:00", which the
    # admin-ui zod HH:MM mirror rejects on form.reset(profile.draft_config).
    from usan_api.schemas.agent_config import PolicyConfig

    dumped = PolicyConfig(quiet_hours_start_local="09:30").model_dump()
    assert dumped["quiet_hours_start_local"] == "09:30"
    assert isinstance(dumped["quiet_hours_start_local"], str)


def test_retry_overrides_bounds():
    from usan_api.schemas.agent_config import PolicyConfig, RetryMaxAttempts

    with pytest.raises(ValidationError):
        PolicyConfig(retry_delay_multiplier=0.4)
    with pytest.raises(ValidationError):
        PolicyConfig(retry_delay_multiplier=4.1)
    assert PolicyConfig(retry_delay_multiplier=0.5).retry_delay_multiplier == 0.5
    assert PolicyConfig(retry_delay_multiplier=4.0).retry_delay_multiplier == 4.0
    for field in ("no_answer", "voicemail_left", "busy", "failed"):
        with pytest.raises(ValidationError):
            RetryMaxAttempts(**{field: -1})
        with pytest.raises(ValidationError):
            RetryMaxAttempts(**{field: 5})
        assert getattr(RetryMaxAttempts(**{field: 0}), field) == 0
        assert getattr(RetryMaxAttempts(**{field: 4}), field) == 4


def test_agent_config_policy_optional_default_none():
    # Forward-compat invariant (the AgentConfig comment block): extends
    # test_legacy_config_still_deserializes — a prompts-only legacy dict (no
    # `policy` key) must keep validating, with policy defaulting to None, or
    # older agent_profile_versions snapshots would 500 on read.
    legacy = {"prompts": DEFAULT_AGENT_CONFIG.prompts.model_dump()}
    cfg = AgentConfig.model_validate(legacy)
    assert cfg.policy is None
