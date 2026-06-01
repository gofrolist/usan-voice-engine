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

VOICEMAIL_MESSAGE = (
    "Hello, this is your daily check-in from USAN Retirement. "
    "We're sorry we missed you. We'll try again a little later. "
    "Take care, and have a wonderful day."
)

STT_MODEL = "ink-whisper"
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
            api_key=settings.gemini_api_key,
        ),
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


async def greet(session: AgentSession[Any]) -> None:
    """Speak the opening greeting once the session is connected."""
    await session.say(GREETING, allow_interruptions=True)
