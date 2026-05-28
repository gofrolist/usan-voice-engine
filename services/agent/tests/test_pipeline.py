from usan_agent.pipeline import (
    GREETING,
    LLM_MODEL,
    STT_MODEL,
    SYSTEM_PROMPT,
    build_agent,
)


def test_build_agent_uses_system_prompt():
    agent = build_agent()
    assert type(agent).__name__ == "Agent"
    assert agent.instructions == SYSTEM_PROMPT


def test_greeting_is_non_empty_and_branded():
    assert GREETING.strip()
    assert "USAN" in GREETING


def test_model_constants():
    assert STT_MODEL == "ink-whisper"
    assert LLM_MODEL.startswith("gemini")
