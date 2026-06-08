"""Factory for the LiveKit Agents 1.x voice pipeline.

The session is built from a resolved AgentConfig (admin-editable), falling back to
DEFAULT_AGENT_CONFIG (the agent's single source of default truth). Optional knobs are
passed only when set, so unset values preserve each plugin's own default. The session
can carry per-call check-in state (userdata) so the outbound agent's tools can act
during the call; inbound greet-only stays tool-less.
"""

from typing import Any

from livekit.agents import AgentSession, ChatContext
from livekit.agents.voice import Agent
from livekit.plugins import cartesia, google, silero
from livekit.plugins.turn_detector.english import EnglishModel
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from loguru import logger

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.settings import Settings

# Back-compat module aliases — the current defaults, sourced from DEFAULT_AGENT_CONFIG
# so there is a single in-agent source of truth (no drift).
SYSTEM_PROMPT = DEFAULT_AGENT_CONFIG.prompts.system_prompt
GREETING = DEFAULT_AGENT_CONFIG.prompts.greeting
RECORDING_DISCLOSURE = DEFAULT_AGENT_CONFIG.prompts.recording_disclosure
VOICEMAIL_MESSAGE = DEFAULT_AGENT_CONFIG.prompts.voicemail_message
STT_MODEL = DEFAULT_AGENT_CONFIG.stt.model
LLM_MODEL = DEFAULT_AGENT_CONFIG.llm.model


def _build_turn_detection(mode: str | None) -> Any:
    """Map the config's turn_detection to a LiveKit turn-detector (or the "vad" mode).

    "english"/None preserve today's EnglishModel default.
    """
    if mode == "multilingual":
        return MultilingualModel()
    if mode == "vad":
        return "vad"
    return EnglishModel()


def build_session(
    settings: Settings, cfg: AgentConfig | None = None, userdata: Any = None
) -> AgentSession[Any]:
    """Construct an AgentSession from a resolved config, wiring STT/LLM/TTS/VAD/turn-detector.

    ``cfg`` defaults to DEFAULT_AGENT_CONFIG. ``userdata`` (a check_in.CheckInData on
    check-in calls) is exposed to tools via RunContext.userdata; None for greet-only.
    Optional knobs are passed only when non-None to preserve plugin defaults.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    sa = cfg.speech_advanced
    logger.info("Building AgentSession ({model})", model=cfg.llm.model)

    vad_kwargs: dict[str, Any] = {}
    if sa.vad_min_silence_s is not None:
        vad_kwargs["min_silence_duration"] = sa.vad_min_silence_s
    if sa.vad_activation_threshold is not None:
        vad_kwargs["activation_threshold"] = sa.vad_activation_threshold

    stt_kwargs: dict[str, Any] = {"model": cfg.stt.model, "api_key": settings.cartesia_api_key}
    if cfg.stt.language is not None:
        stt_kwargs["language"] = cfg.stt.language

    llm_kwargs: dict[str, Any] = {
        "model": cfg.llm.model,
        "vertexai": True,
        "project": settings.gcp_project,
        "location": settings.vertex_location,
    }
    if cfg.llm.temperature is not None:
        llm_kwargs["temperature"] = cfg.llm.temperature

    tts_kwargs: dict[str, Any] = {
        "voice": cfg.voice.cartesia_voice_id or settings.default_cartesia_voice_id,
        "api_key": settings.cartesia_api_key,
    }
    if cfg.voice.tts_model is not None:
        tts_kwargs["model"] = cfg.voice.tts_model
    if cfg.voice.speed is not None:
        tts_kwargs["speed"] = cfg.voice.speed
    if cfg.voice.language is not None:
        tts_kwargs["language"] = cfg.voice.language

    session_kwargs: dict[str, Any] = {
        "userdata": userdata,
        "vad": silero.VAD.load(**vad_kwargs),
        "stt": cartesia.STT(**stt_kwargs),
        # no api_key on the LLM → ADC via the attached VM service account (Vertex AI,
        # BAA-covered). project/location stay in settings (infra/BAA config).
        "llm": google.LLM(**llm_kwargs),
        "tts": cartesia.TTS(**tts_kwargs),
        "turn_detection": _build_turn_detection(sa.turn_detection),
    }
    if sa.min_endpointing_delay_s is not None:
        session_kwargs["min_endpointing_delay"] = sa.min_endpointing_delay_s
    if sa.max_endpointing_delay_s is not None:
        session_kwargs["max_endpointing_delay"] = sa.max_endpointing_delay_s
    if sa.min_interruption_duration_s is not None:
        session_kwargs["min_interruption_duration"] = sa.min_interruption_duration_s
    if sa.min_interruption_words is not None:
        session_kwargs["min_interruption_words"] = sa.min_interruption_words

    return AgentSession(**session_kwargs)


def build_agent(cfg: AgentConfig | None = None) -> Agent:
    """Construct the greet-only Agent with the configured system prompt (no tools)."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=cfg.prompts.system_prompt,
        chat_ctx=ChatContext(),
    )


async def say_recording_disclosure(
    session: AgentSession[Any], cfg: AgentConfig | None = None
) -> None:
    """Speak the non-interruptible recording disclosure (spec §10) to completion.

    Awaiting this before starting egress guarantees the consent notice is heard
    before any audio is captured.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    await session.say(
        cfg.prompts.recording_disclosure, allow_interruptions=False, add_to_chat_ctx=False
    )


async def greet(
    session: AgentSession[Any],
    cfg: AgentConfig | None = None,
    *,
    include_disclosure: bool = True,
) -> None:
    """Speak the recording disclosure (spec §10), then the opening greeting.

    ``include_disclosure=False`` skips the disclosure when the caller has split it
    out to gate egress on consent (outbound), so it is never spoken twice.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    if include_disclosure:
        await say_recording_disclosure(session, cfg)
    await session.say(cfg.prompts.greeting, allow_interruptions=True)
