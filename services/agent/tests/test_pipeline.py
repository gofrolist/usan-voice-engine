from types import SimpleNamespace

from usan_agent import pipeline as pipeline_mod
from usan_agent.pipeline import (
    GREETING,
    LLM_MODEL,
    STT_MODEL,
    SYSTEM_PROMPT,
    build_agent,
    build_session,
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


def test_build_session_uses_vertex_not_developer_api(monkeypatch):
    # The LLM must run on Vertex AI (BAA-covered): vertexai=True + project + location,
    # and NO api_key (ADC via the attached VM service account). Guards against
    # regressing to the non-HIPAA Gemini Developer API. See Plan 4e Task A1.
    captured: dict = {}

    class _Stub:
        pass

    def _fake_llm(**kwargs):
        captured.update(kwargs)
        return _Stub()

    monkeypatch.setattr(pipeline_mod.google, "LLM", _fake_llm)
    monkeypatch.setattr(pipeline_mod.silero.VAD, "load", lambda *a, **k: _Stub())
    monkeypatch.setattr(pipeline_mod.cartesia, "STT", lambda **k: _Stub())
    monkeypatch.setattr(pipeline_mod.cartesia, "TTS", lambda **k: _Stub())
    monkeypatch.setattr(pipeline_mod, "EnglishModel", lambda *a, **k: _Stub())
    monkeypatch.setattr(pipeline_mod, "AgentSession", lambda **k: _Stub())

    settings = SimpleNamespace(
        cartesia_api_key="c",
        default_cartesia_voice_id="v",
        gcp_project="usan-retirement",
        vertex_location="global",
    )
    build_session(settings)

    assert captured.get("vertexai") is True
    assert captured.get("project") == "usan-retirement"
    assert captured.get("location") == "global"
    assert "api_key" not in captured
    assert captured.get("model") == LLM_MODEL
