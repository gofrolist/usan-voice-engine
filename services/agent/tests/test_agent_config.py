from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig


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
        "schedule_callback",
        "send_sms",
        "end_call",
    ]
    assert cfg.voicemail_detection.window_s == 3.0
    assert cfg.voicemail_detection.trigger_phrases == []


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
            "inbound_personalization_template": "hello {elder_name} {last_check_in_line}",
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
