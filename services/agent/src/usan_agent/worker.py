"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

import json
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.settings import get_settings


@dataclass(frozen=True)
class CallMetadata:
    """Per-call context passed by the API via dispatch metadata.

    Inbound dispatch-rule jobs carry no metadata, so absence means inbound.
    """

    call_id: str | None
    direction: str
    dynamic_vars: dict[str, Any] = field(default_factory=dict)


def parse_metadata(raw: str | None) -> CallMetadata:
    if not raw:
        return CallMetadata(call_id=None, direction="inbound", dynamic_vars={})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse job metadata as JSON; treating as inbound")
        return CallMetadata(call_id=None, direction="inbound", dynamic_vars={})
    return CallMetadata(
        call_id=data.get("call_id"),
        direction=data.get("direction", "inbound"),
        dynamic_vars=data.get("dynamic_vars") or {},
    )


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    meta = parse_metadata(ctx.job.metadata)
    log = logger.bind(room=ctx.room.name, call_id=meta.call_id, direction=meta.direction)
    log.info("Job assigned, connecting to room")

    await ctx.connect()
    log.info("Connected to room")

    session = build_session(settings)
    agent = build_agent()

    await session.start(agent=agent, room=ctx.room)
    log.info("Session started; waiting for participant")

    # Inbound: the caller is already present and this returns immediately.
    # Outbound: blocks until the callee answers and the SIP participant joins.
    await ctx.wait_for_participant()
    log.info("Participant present; greeting")

    await greet(session)
    log.info("Greeting spoken")


def main() -> None:
    # Configure logging first so a missing/invalid-env failure in get_settings()
    # is emitted as a structured log line, not a raw traceback.
    configure_logging()
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting USAN agent worker (agent_name={name})", name=settings.agent_name)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=settings.agent_name,
        )
    )


if __name__ == "__main__":
    main()
