"""Factory for the LiveKit Agents 1.x voice pipeline.

Plan 1 scope: hardcoded greeting + single-turn loop, no tools.
"""

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


def build_session(settings: Settings) -> AgentSession:
    """Construct an AgentSession wiring STT, LLM, TTS, VAD, and turn-detector."""
    logger.info("Building AgentSession (cartesia STT/TTS, gemini-3.1-flash-lite)")
    return AgentSession(
        vad=silero.VAD.load(),
        stt=cartesia.STT(
            model="ink-whisper",
            api_key=settings.cartesia_api_key,
        ),
        llm=google.LLM(
            model="gemini-3.1-flash-lite",
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


async def greet(session: AgentSession) -> None:
    """Speak the opening greeting once the session is connected."""
    await session.say(GREETING, allow_interruptions=True)
