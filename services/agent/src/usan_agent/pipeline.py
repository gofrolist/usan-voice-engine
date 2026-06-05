"""Factory for the LiveKit Agents 1.x voice pipeline.

The session can carry per-call check-in state (userdata) so the outbound agent's
tools can act during the call; inbound stays greet-only.
"""

from typing import Any

from livekit.agents import AgentSession, ChatContext
from livekit.agents.voice import Agent
from livekit.plugins import cartesia, google, silero
from livekit.plugins.turn_detector.english import EnglishModel
from loguru import logger

from usan_agent.settings import Settings

SYSTEM_PROMPT = """You are a warm, patient daily check-in assistant from USAN Retirement.
You are speaking to an elder over the phone. Speak slowly, clearly, and kindly.
Keep responses short — one or two sentences. Pause to let them respond.
"""

GREETING = "Hello! This is your daily check-in from USAN. How are you feeling today?"

RECORDING_DISCLOSURE = (
    "Before we begin, please know that this call is recorded for quality and to support your care."
)

VOICEMAIL_MESSAGE = (
    "Hello, this is your daily check-in from USAN Retirement. "
    "We're sorry we missed you. We'll try again a little later. "
    "Take care, and have a wonderful day."
)

STT_MODEL = "ink-whisper"
# Served on Vertex AI (HIPAA-BAA-covered) via the GLOBAL endpoint — verified: 200 on
# location=global, 404 on regional us-east1/us-central1. So VERTEX_LOCATION must be
# "global" for this model (set in settings). See Plan 4e Task A1.
LLM_MODEL = "gemini-3.1-flash-lite"


def build_session(settings: Settings, userdata: Any = None) -> AgentSession[Any]:
    """Construct an AgentSession wiring STT, LLM, TTS, VAD, and turn-detector.

    ``userdata`` (a check_in.CheckInData on outbound calls) is exposed to tools via
    RunContext.userdata; None for greet-only inbound calls.
    """
    logger.info("Building AgentSession (cartesia STT/TTS, {model})", model=LLM_MODEL)
    return AgentSession(
        userdata=userdata,
        vad=silero.VAD.load(),
        stt=cartesia.STT(
            model=STT_MODEL,
            api_key=settings.cartesia_api_key,
        ),
        llm=google.LLM(
            model=LLM_MODEL,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
        ),  # no api_key → ADC via the attached VM service account (Vertex AI, BAA-covered)
        tts=cartesia.TTS(
            voice=settings.default_cartesia_voice_id,
            api_key=settings.cartesia_api_key,
        ),
        turn_detection=EnglishModel(),
    )


def build_agent() -> Agent:
    """Construct the Agent with system prompt."""
    return Agent(
        instructions=SYSTEM_PROMPT,
        chat_ctx=ChatContext(),
    )


async def say_recording_disclosure(session: AgentSession[Any]) -> None:
    """Speak the non-interruptible recording disclosure (spec §10) to completion.

    Awaiting this before starting egress guarantees the consent notice is heard
    before any audio is captured.
    """
    await session.say(RECORDING_DISCLOSURE, allow_interruptions=False, add_to_chat_ctx=False)


async def greet(session: AgentSession[Any], *, include_disclosure: bool = True) -> None:
    """Speak the recording disclosure (spec §10), then the opening greeting.

    ``include_disclosure=False`` skips the disclosure when the caller has split it
    out to gate egress on consent (outbound), so it is never spoken twice.
    """
    if include_disclosure:
        await say_recording_disclosure(session)
    await session.say(GREETING, allow_interruptions=True)
