from usan_agent import check_in, pipeline
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG


def test_default_prompts_match_pipeline_constants():
    p = DEFAULT_AGENT_CONFIG.prompts
    assert p.system_prompt == pipeline.SYSTEM_PROMPT
    assert p.greeting == pipeline.GREETING
    assert p.recording_disclosure == pipeline.RECORDING_DISCLOSURE
    assert p.voicemail_message == pipeline.VOICEMAIL_MESSAGE


def test_default_prompts_match_check_in_constants():
    p = DEFAULT_AGENT_CONFIG.prompts
    assert p.checkin_flow_instructions == check_in.CHECK_IN_INSTRUCTIONS
    assert p.goodbye_message == check_in.GOODBYE_MESSAGE
    assert p.inbound_personalization_template == check_in.INBOUND_INSTRUCTIONS_TEMPLATE


def test_default_inbound_opening_is_present():
    # The worker no longer holds its own _INBOUND_OPENING constant; the inbound
    # opening now lives canonically in the config and is threaded via cfg.
    opening = DEFAULT_AGENT_CONFIG.prompts.inbound_opening
    assert isinstance(opening, str)
    assert opening.strip()


def test_default_models_match_pipeline_constants():
    assert DEFAULT_AGENT_CONFIG.llm.model == pipeline.LLM_MODEL
    assert DEFAULT_AGENT_CONFIG.stt.model == pipeline.STT_MODEL
