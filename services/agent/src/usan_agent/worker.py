"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.settings import get_settings


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    logger.bind(room=ctx.room.name).info("Job assigned, connecting to room")

    await ctx.connect()
    logger.bind(room=ctx.room.name).info("Connected to room")

    session = build_session(settings)
    agent = build_agent()

    await session.start(agent=agent, room=ctx.room)
    logger.bind(room=ctx.room.name).info("Session started")

    await greet(session)
    logger.bind(room=ctx.room.name).info("Greeting spoken")


def main() -> None:
    # Configure logging first so a missing/invalid-env failure in get_settings()
    # is emitted as a structured log line, not a raw traceback.
    configure_logging()
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting USAN agent worker")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # No agent_name = picked up by any dispatch rule in the project
        )
    )


if __name__ == "__main__":
    main()
