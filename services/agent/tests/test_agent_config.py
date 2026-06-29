from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, LLMConfig


def test_default_is_complete_and_branded():
    cfg = DEFAULT_AGENT_CONFIG
    assert "USAN" in cfg.prompts.greeting
    assert cfg.prompts.system_prompt.startswith("You are a warm")
    assert "log_wellness" in cfg.prompts.checkin_flow_instructions
    assert cfg.llm.model.startswith("gemini")
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
    assert cfg.voicemail_detection.window_s == 3.0
    assert cfg.voicemail_detection.trigger_phrases == []
    # T069 / FR-036 + US8 / FR-040: the agent-side prompt mirror carries the anti-scam
    # guidance and the opt-out / info-sms / Spanish-callback tool wiring in both prompt
    # blocks (parity with apps/api).
    for block in (
        cfg.prompts.checkin_flow_instructions,
        cfg.prompts.inbound_personalization_template,
    ):
        assert "SCAM AWARENESS" in block
        assert "register_opt_out" in block
        assert "send_info_sms" in block
        assert "SPANISH" in block
        assert "set_spanish_callback" in block


def test_parse_minimal_prompts_only_document():
    # The server always sends a full document, but parsing must succeed from just the
    # required prompts block, defaulting every optional sub-config.
    doc = {
        "prompts": {
            "system_prompt": "sys",
            "greeting": "hi",
            "recording_disclosure": "rec",
            "voicemail_message": "vm",
            "checkin_flow_instructions": "flow",
            "goodbye_message": "bye",
            "inbound_opening": "open",
            "inbound_personalization_template": "hello {contact_name} {last_check_in_line}",
        }
    }
    cfg = AgentConfig.model_validate(doc)
    assert cfg.voice.cartesia_voice_id is None
    assert cfg.llm.model.startswith("gemini")  # default applied
    assert cfg.speech_advanced.turn_detection is None


def test_parse_ignores_unknown_fields():
    doc = DEFAULT_AGENT_CONFIG.model_dump()
    doc["prompts"]["a_future_field"] = "ignored"
    doc["a_future_top_level"] = {"x": 1}
    cfg = AgentConfig.model_validate(doc)  # must not raise
    assert cfg.prompts.greeting == DEFAULT_AGENT_CONFIG.prompts.greeting


def test_roundtrip_dump_then_validate():
    cfg = AgentConfig.model_validate(DEFAULT_AGENT_CONFIG.model_dump())
    assert cfg == DEFAULT_AGENT_CONFIG


def test_agent_sms_config_parses_without_validators():
    # Agent mirror is parse-only: it accepts ANY body (even a PHI token) because the
    # API already validated on the write path. No validator may reject here.
    from usan_agent.agent_config import SmsTemplate, SmsToolConfig, ToolsConfig

    cfg = ToolsConfig(
        enabled=list(DEFAULT_AGENT_CONFIG.tools.enabled),
        sms=SmsToolConfig(templates=[SmsTemplate(key="x", label="X", body="Status {{last_mood}}")]),
    )
    assert cfg.sms is not None
    assert cfg.sms.templates[0].key == "x"


def test_agent_sms_default_is_none():
    from usan_agent.agent_config import ToolsConfig

    assert ToolsConfig().sms is None


def test_agent_config_roundtrips_sms_block():
    base = DEFAULT_AGENT_CONFIG.model_dump()
    base["tools"] = {
        "enabled": list(DEFAULT_AGENT_CONFIG.tools.enabled),
        "sms": {"templates": [{"key": "a", "label": "A", "body": "Hi {{first_name}}"}]},
    }
    cfg = AgentConfig.model_validate(base)
    assert cfg.tools.sms is not None
    assert cfg.tools.sms.templates[0].key == "a"


def test_llm_config_knowledge_base_ids_defaults_none():
    assert LLMConfig().knowledge_base_ids is None


def test_llm_config_parses_knowledge_base_ids():
    cfg = LLMConfig.model_validate({"model": "m", "knowledge_base_ids": ["knowledge_base_a"]})
    assert cfg.knowledge_base_ids == ["knowledge_base_a"]


def test_old_config_without_knowledge_base_ids_still_validates():
    # Forward-compat: a published config produced before the field existed must parse.
    raw = DEFAULT_AGENT_CONFIG.model_dump()
    raw["llm"].pop("knowledge_base_ids", None)
    cfg = AgentConfig.model_validate(raw)
    assert cfg.llm.knowledge_base_ids is None


def test_unknown_policy_key_is_ignored():
    # Regression pin: the API-side AgentConfig grows an optional `policy` section
    # (quiet-hours narrowing + retry overrides) consumed exclusively server-side.
    # The agent mirror must keep tolerating that riding-along key via pydantic's
    # default extra="ignore" (agent_config.py:95 sets only frozen=True) — a future
    # extra="forbid" cleanup would break runtime config fetch for every
    # policy-carrying profile (spec §3.3.1).
    payload = {
        "prompts": {
            "system_prompt": "sys",
            "greeting": "hi",
            "recording_disclosure": "rec",
            "voicemail_message": "vm",
            "checkin_flow_instructions": "flow",
            "goodbye_message": "bye",
            "inbound_opening": "open",
            "inbound_personalization_template": "hello {contact_name} {last_check_in_line}",
        },
        "policy": {"quiet_hours_start_local": "10:00"},
    }
    cfg = AgentConfig.model_validate(payload)  # must not raise
    assert not hasattr(cfg, "policy")
