from types import SimpleNamespace

from livekit.agents.voice import Agent

from usan_agent import pipeline as pipeline_mod
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.pipeline import (
    GREETING,
    LLM_MODEL,
    STT_MODEL,
    SYSTEM_PROMPT,
    build_agent,
    build_session,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        cartesia_api_key="c",
        default_cartesia_voice_id="env-voice",
        gcp_project="usan-retirement",
        vertex_location="global",
    )


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class _Stub:
        pass

    def _grab(key):
        def _factory(*a, **k):
            captured[key] = k
            return _Stub()

        return _factory

    monkeypatch.setattr(pipeline_mod.google, "LLM", _grab("llm"))
    monkeypatch.setattr(pipeline_mod.cartesia, "STT", _grab("stt"))
    monkeypatch.setattr(pipeline_mod.cartesia, "TTS", _grab("tts"))
    monkeypatch.setattr(pipeline_mod.silero.VAD, "load", _grab("vad"))
    monkeypatch.setattr(pipeline_mod, "AgentSession", _grab("session"))
    monkeypatch.setattr(pipeline_mod, "EnglishModel", lambda *a, **k: _Stub())
    monkeypatch.setattr(pipeline_mod, "MultilingualModel", lambda *a, **k: _Stub())
    return captured


def test_build_agent_uses_default_system_prompt():
    agent = build_agent()
    assert isinstance(agent, Agent)
    assert agent.instructions == SYSTEM_PROMPT


def test_greeting_is_non_empty_and_branded():
    assert GREETING.strip()
    assert "USAN" in GREETING


def test_model_constants():
    assert STT_MODEL == "ink-whisper"
    assert LLM_MODEL.startswith("gemini")


def test_build_session_uses_vertex_not_developer_api(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings(), DEFAULT_AGENT_CONFIG)
    llm = captured["llm"]
    assert llm.get("vertexai") is True
    assert llm.get("project") == "usan-retirement"
    assert llm.get("location") == "global"
    assert "api_key" not in llm
    assert llm.get("model") == LLM_MODEL


def test_build_session_defaults_omit_optional_kwargs(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings(), DEFAULT_AGENT_CONFIG)
    assert captured["stt"].get("model") == "ink-whisper"
    assert "language" not in captured["stt"]
    assert "temperature" not in captured["llm"]
    assert captured["tts"].get("voice") == "env-voice"  # falls back to settings default
    assert "speed" not in captured["tts"]
    assert "model" not in captured["tts"]
    assert "language" not in captured["tts"]
    assert captured["vad"] == {}
    sess = captured["session"]
    for k in (
        "min_endpointing_delay",
        "max_endpointing_delay",
        "min_interruption_duration",
        "min_interruption_words",
    ):
        assert k not in sess


def test_build_session_applies_voice_llm_stt(monkeypatch):
    captured = _capture(monkeypatch)
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "voice": {
                "cartesia_voice_id": "cfg-voice",
                "tts_model": "sonic-2",
                "speed": 1.2,
                "language": "es",
            },
            "llm": {"model": "gemini-3.1-pro", "temperature": 0.4},
            "stt": {"model": "ink-whisper", "language": "es"},
        }
    )
    build_session(_settings(), cfg)
    assert captured["tts"].get("voice") == "cfg-voice"
    assert captured["tts"].get("model") == "sonic-2"
    assert captured["tts"].get("speed") == 1.2
    assert captured["tts"].get("language") == "es"
    assert captured["llm"].get("model") == "gemini-3.1-pro"
    assert captured["llm"].get("temperature") == 0.4
    assert captured["stt"].get("language") == "es"


def test_build_session_applies_speech_advanced(monkeypatch):
    captured = _capture(monkeypatch)
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "speech_advanced": {
                "vad_min_silence_s": 0.8,
                "vad_activation_threshold": 0.6,
                "turn_detection": "multilingual",
                "min_endpointing_delay_s": 0.5,
                "max_endpointing_delay_s": 6.0,
                "min_interruption_duration_s": 0.4,
                "min_interruption_words": 2,
            },
        }
    )
    build_session(_settings(), cfg)
    assert captured["vad"].get("min_silence_duration") == 0.8
    assert captured["vad"].get("activation_threshold") == 0.6
    sess = captured["session"]
    assert sess.get("min_endpointing_delay") == 0.5
    assert sess.get("max_endpointing_delay") == 6.0
    assert sess.get("min_interruption_duration") == 0.4
    assert sess.get("min_interruption_words") == 2


def test_build_turn_detection_modes(monkeypatch):
    _capture(monkeypatch)
    assert pipeline_mod._build_turn_detection("vad") == "vad"
    # english and None both yield the EnglishModel default (not the "vad" string)
    assert pipeline_mod._build_turn_detection("english") != "vad"
    assert pipeline_mod._build_turn_detection(None) != "vad"
    assert pipeline_mod._build_turn_detection("multilingual") != "vad"


def test_build_session_defaults_to_default_config(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings())  # no cfg -> DEFAULT_AGENT_CONFIG
    assert captured["llm"].get("model") == LLM_MODEL
