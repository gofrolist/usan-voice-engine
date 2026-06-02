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

from usan_agent.api_client import start_inbound_call
from usan_agent.check_in import CheckInData, build_check_in_agent, build_inbound_agent
from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.recording import start_call_recording
from usan_agent.settings import Settings, get_settings
from usan_agent.transcript import register_transcript_flush
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


_INBOUND_OPENING = (
    "Greet the caller warmly by name if you know it, and ask how they are feeling "
    "today to begin the daily check-in."
)


def _caller_phone(participant: Any) -> str | None:
    """Read the inbound caller's E.164 number from the SIP participant attributes.

    livekit-sip populates ``sip.phoneNumber`` with the remote party's number; on
    inbound that is the caller. ``sip.from`` is a fallback on newer sip servers.
    """
    attrs = getattr(participant, "attributes", None) or {}
    return attrs.get("sip.phoneNumber") or attrs.get("sip.from") or None


async def _run_inbound(ctx: JobContext, settings: Settings, log: Any) -> None:
    """Inbound: wait for the caller, look them up, run a personalized check-in.

    No voicemail detection on inbound (spec §7). A known elder gets the tool-driven
    check-in with a personalized opening + transcript flush; an unknown number or a
    failed lookup falls back to a greet-only conversation (no per-elder state, so no
    orphaned wellness/medication logs).
    """
    participant = await ctx.wait_for_participant()
    phone = _caller_phone(participant)
    log.info("Inbound caller present (phone={phone})", phone=phone)

    # The lookup precedes session.start, so the caller hears a brief silence (the
    # API round-trip) before the agent speaks. Acceptable in v1 because the agent
    # greets first — the caller is not yet expected to be speaking. If zero-gap
    # audio capture is ever needed, start the session first and reconfigure the
    # agent after the lookup.
    info = await start_inbound_call(phone, ctx.room.name, settings)
    if info and info.get("elder_known") and info.get("call_id"):
        call_id = str(info["call_id"])
        await start_call_recording(ctx, call_id, settings)
        dynamic_vars = info.get("dynamic_vars") or {}
        data = CheckInData(call_id=call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_inbound_agent(dynamic_vars)
        register_transcript_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        log.info("Inbound check-in started for known elder (call_id={cid})", cid=call_id)
        await session.generate_reply(instructions=_INBOUND_OPENING)
        return

    # Unknown caller or lookup failed: greet-only, no per-elder state.
    session = build_session(settings)
    agent = build_agent()
    await session.start(agent=agent, room=ctx.room)
    log.info("Inbound greet-only (no known elder)")
    await greet(session)


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
        await start_call_recording(ctx, meta.call_id, settings)
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_check_in_agent()
        register_transcript_flush(ctx, session, meta.call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        log.info("Session started; waiting for participant")
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

    # Inbound: caller already dialed in; no voicemail detection (spec §7).
    await _run_inbound(ctx, settings, log)


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
