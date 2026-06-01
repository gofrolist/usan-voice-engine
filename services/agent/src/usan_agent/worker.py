"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent.check_in import CheckInData, build_check_in_agent
from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.settings import Settings, get_settings
from usan_agent.voicemail import VOICEMAIL_WINDOW_S, VoicemailWatcher
from usan_agent.voicemail_action import leave_voicemail


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


async def _run_detection_window(
    ctx: JobContext,
    session: Any,
    watcher: VoicemailWatcher,
    *,
    call_id: str | None,
    settings: Settings,
) -> None:
    """Greet, then over the detection window leave a voicemail or fall through."""
    # The watcher is already subscribed to user_input_transcribed (in entrypoint),
    # so a voicemail greeting spoken DURING this greeting's playout still feeds the
    # watcher; wait_until_detected returns immediately if the event is already set.
    await greet(session)
    if await watcher.wait_until_detected(VOICEMAIL_WINDOW_S):
        await leave_voicemail(ctx, session, call_id, settings)
    # else: a human answered — the conversation continues (single-turn in Plan 1).


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    meta = parse_metadata(ctx.job.metadata)
    log = logger.bind(room=ctx.room.name, call_id=meta.call_id, direction=meta.direction)
    log.info("Job assigned, connecting to room")

    await ctx.connect()
    log.info("Connected to room")

    if meta.direction == "outbound" and meta.call_id:
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_check_in_agent()
    else:
        session = build_session(settings)
        agent = build_agent()
    await session.start(agent=agent, room=ctx.room)
    log.info("Session started; waiting for participant")

    if meta.direction == "outbound":
        try:
            await asyncio.wait_for(
                ctx.wait_for_participant(), timeout=settings.outbound_answer_timeout_s
            )
        except TimeoutError:
            # asyncio.TimeoutError is an alias of builtin TimeoutError on 3.11+.
            # The API's dial task classifies/cleans up no-answer; this is the
            # agent-side backstop so the job never hangs on an unanswered call.
            log.info("No participant within answer timeout; ending job")
            ctx.shutdown(reason="no_answer_timeout")
            return

        watcher = VoicemailWatcher()
        session.on("user_input_transcribed", lambda ev: watcher.feed(ev.transcript))
        log.info("Participant present; running voicemail detection window")
        await _run_detection_window(ctx, session, watcher, call_id=meta.call_id, settings=settings)
        return

    # Inbound: caller already present; no voicemail detection (spec §7).
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
