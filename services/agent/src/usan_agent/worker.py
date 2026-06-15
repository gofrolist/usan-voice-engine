"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent import api_client
from usan_agent.agent_config import AgentConfig
from usan_agent.api_client import CrisisCategory, fetch_agent_config, start_inbound_call
from usan_agent.check_in import (
    CheckInData,
    build_check_in_agent,
    build_inbound_agent,
    build_test_agent,
)
from usan_agent.crisis_watcher import CrisisWatcher
from usan_agent.ids import validate_call_id
from usan_agent.logging_config import configure_logging
from usan_agent.metrics_hooks import register_metrics_flush
from usan_agent.pipeline import build_agent, build_session, greet, say_recording_disclosure
from usan_agent.recording import start_call_recording
from usan_agent.settings import Settings, get_settings
from usan_agent.transcript import register_transcript_flush
from usan_agent.voicemail import VoicemailWatcher, build_matcher
from usan_agent.voicemail_action import leave_voicemail

# Strong references to fire-and-forget background tasks (the max-duration guard).
# asyncio only holds a weak reference to a bare create_task() result, so without
# this the GC may collect a still-running task and silently cancel it. Each task
# removes itself via add_done_callback when it finishes.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class CallMetadata:
    """Per-call context passed by the API via dispatch metadata.

    Inbound dispatch-rule jobs carry no metadata, so absence means inbound.
    ``resolved_vars`` holds the API-resolved DATA built-ins; ``timezone`` is the
    contact's IANA tz string.  The agent adds ``current_time``/``current_date``
    from ``timezone`` via ``build_vars``.  ``dynamic_vars`` stays the operator's
    custom map (the idempotency payload — never merged with built-ins).
    """

    call_id: str | None
    direction: str
    dynamic_vars: dict[str, Any] = field(default_factory=dict)
    resolved_vars: dict[str, str] = field(default_factory=dict)
    timezone: str = ""
    # Sandbox discriminator (US5 / contract agent-test-session.md). Absent on every
    # existing dispatch → "call" (byte-compatible). "test" selects the pre-publish
    # Test Audio branch; ``test_config`` then carries the full draft AgentConfig doc.
    session_kind: str = "call"
    test_config: dict[str, Any] | None = None


def parse_metadata(raw: str | None) -> CallMetadata:
    if not raw:
        return CallMetadata(call_id=None, direction="inbound")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse job metadata as JSON; treating as inbound")
        return CallMetadata(call_id=None, direction="inbound")
    # Default "call" keeps every existing outbound/inbound dispatch unchanged.
    session_kind = data.get("session_kind") or "call"
    return CallMetadata(
        call_id=data.get("call_id"),
        direction=data.get("direction", "inbound"),
        dynamic_vars=data.get("dynamic_vars") or {},
        resolved_vars=data.get("resolved_vars") or {},
        timezone=data.get("timezone") or "",
        session_kind="test" if session_kind == "test" else "call",
        test_config=data.get("test_config"),
    )


def _caller_phone(participant: Any) -> str | None:
    """Read the inbound caller's E.164 number from the SIP participant attributes.

    livekit-sip populates ``sip.phoneNumber`` with the remote party's number; on
    inbound that is the caller. ``sip.from`` is a fallback on newer sip servers.
    """
    attrs = getattr(participant, "attributes", None) or {}
    return attrs.get("sip.phoneNumber") or attrs.get("sip.from") or None


def _mask_phone(phone: str | None) -> str:
    """Mask a caller's phone for logs: keep only the last 4 digits (PHI minimization).

    A full E.164 number identifies a patient, so it must never reach the logs.
    """
    if not phone:
        return "unknown"
    return "***" + phone[-4:]


async def _run_inbound(ctx: JobContext, settings: Settings, cfg: AgentConfig, log: Any) -> None:
    """Inbound: wait for the caller, look them up, run a personalized check-in.

    Uses the inbound default config (cfg). No voicemail detection on inbound (spec §7).
    A known contact gets the tool-driven check-in with a personalized opening + transcript
    flush; an unknown number or a failed lookup falls back to a greet-only conversation
    (no per-contact state, so no orphaned wellness/medication logs).
    """
    participant = await ctx.wait_for_participant()
    phone = _caller_phone(participant)
    log.info("Inbound caller present (phone={phone})", phone=_mask_phone(phone))

    # The lookup precedes session.start, so the caller hears a brief silence (the
    # API round-trip) before the agent speaks. Acceptable in v1 because the agent
    # greets first — the caller is not yet expected to be speaking. If zero-gap
    # audio capture is ever needed, start the session first and reconfigure the
    # agent after the lookup.
    info = await start_inbound_call(phone, ctx.room.name, settings)
    if info and info.get("contact_known") and info.get("call_id"):
        call_id = str(info["call_id"])
        dynamic_vars = info.get("dynamic_vars") or {}
        data = CheckInData(
            call_id=call_id,
            settings=settings,
            job_ctx=ctx,
            goodbye_message=cfg.prompts.goodbye_message,
        )
        session = build_session(settings, cfg, userdata=data)
        agent = build_inbound_agent(
            cfg,
            resolved_vars=info.get("resolved_vars") or {},
            custom_vars=dynamic_vars,
            timezone=info.get("timezone") or "",
        )
        register_transcript_flush(ctx, session, call_id, settings)
        register_metrics_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        # Deterministic crisis safety net for inbound known-contact calls too (US1 / FR-002).
        _arm_crisis_safety_net(session, call_id=call_id, settings=settings)
        # Hold a reference so the guard task is not garbage-collected before it
        # completes (asyncio keeps only a weak reference to fire-and-forget tasks).
        _guard_task = asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
        _BACKGROUND_TASKS.add(_guard_task)
        _guard_task.add_done_callback(_BACKGROUND_TASKS.discard)
        log.info("Inbound check-in started for known contact (call_id={cid})", cid=call_id)
        # Consent before capture: the disclosure must finish playing before egress
        # starts, so no audio is recorded prior to the spoken notice (consent ordering).
        await say_recording_disclosure(session, cfg)
        await start_call_recording(ctx, call_id, settings)
        await session.generate_reply(instructions=cfg.prompts.inbound_opening)
        return

    # Unknown caller or lookup failed: greet-only, no per-contact state.
    session = build_session(settings, cfg)
    agent = build_agent(cfg)
    await session.start(agent=agent, room=ctx.room)
    # Arm the max-duration guard on this path too (it backstops the cost/safety cap on
    # every answered call). Hold a reference so the fire-and-forget task is not GC'd.
    _guard_task = asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
    _BACKGROUND_TASKS.add(_guard_task)
    _guard_task.add_done_callback(_BACKGROUND_TASKS.discard)
    log.info("Inbound greet-only (no known contact)")
    await greet(session, cfg)


async def _run_detection_window(
    ctx: JobContext,
    session: Any,
    watcher: VoicemailWatcher,
    *,
    call_id: str | None,
    settings: Settings,
    cfg: AgentConfig,
) -> None:
    """Greet, then over the detection window leave a voicemail or fall through."""
    # The watcher is already subscribed to user_input_transcribed (in entrypoint),
    # so a voicemail greeting spoken DURING this greeting's playout still feeds the
    # watcher; wait_until_detected returns immediately if the event is already set.
    # The recording disclosure was already spoken (gating egress on consent) before
    # this window, so the greeting here must not repeat it.
    await greet(session, cfg, include_disclosure=False)
    if await watcher.wait_until_detected(cfg.voicemail_detection.window_s):
        await leave_voicemail(
            ctx, session, call_id, settings, voicemail_message=cfg.prompts.voicemail_message
        )
    # else: a human answered — the conversation continues (single-turn in Plan 1).


async def _max_duration_guard(ctx: JobContext, max_s: float) -> None:
    """Backstop: shut the job down if a call exceeds its configured max duration.

    A cost/safety cap (also covered API-side for outbound). Nothing cancels this task;
    it is killed by process teardown (process-per-job), so on a normal call the sleep
    simply never elapses and the guard never fires.
    """
    try:
        await asyncio.sleep(max_s)
    except asyncio.CancelledError:
        return
    logger.bind(room=ctx.room.name).warning("Max call duration reached; ending job")
    # Hang up the contact's SIP/PSTN leg first: shutdown() disconnects the agent but
    # leaves the room (and the billable carrier leg) up until empty_timeout. Awaiting
    # matters — delete_room enqueues work that shutdown would otherwise cancel. Mirrors
    # check_in._do_end_call / voicemail_action.leave_voicemail (delete_room then shutdown).
    try:
        await ctx.delete_room()
    except Exception:
        logger.bind(room=ctx.room.name).warning("delete_room failed in max-duration guard")
    ctx.shutdown(reason="max_call_duration")


async def _run_test_session(ctx: JobContext, settings: Settings, meta: CallMetadata) -> None:
    """Sandboxed pre-publish Test Audio session (US5 / FR-027, FR-028).

    Builds the agent from the inline DRAFT ``test_config`` (no published-only
    resolver, no inbound lookup) and registers ONLY the no-op test tool registry, so
    the run writes NO Call/wellness/medication/audit row and makes NO /v1/tools/*
    call. It skips recording/egress and SIP entirely and waits for the browser
    participant generically (no sip.* reads). The existing max-duration watchdog
    bounds the session length.
    """
    log = logger.bind(room=ctx.room.name, kind="test")
    # Build the draft config; an invalid document means a bad dispatch — drop the job.
    try:
        cfg = AgentConfig.model_validate(meta.test_config or {})
    except Exception:
        log.error("Invalid test_config in dispatch metadata; refusing test job")
        ctx.shutdown(reason="invalid_metadata")
        return
    # No CheckInData with a real call_id: a synthetic id keeps the typed userdata
    # shape (the no-op tools never use it to hit the API), and end_call can hang up.
    data = CheckInData(
        call_id="test-session",
        settings=settings,
        job_ctx=ctx,
        goodbye_message=cfg.prompts.goodbye_message,
    )
    session = build_session(settings, cfg, userdata=data)
    agent = build_test_agent(
        cfg,
        resolved_vars=meta.resolved_vars,
        custom_vars=meta.dynamic_vars,
        timezone=meta.timezone,
    )
    await session.start(agent=agent, room=ctx.room)
    log.info("Test session started; waiting for browser participant")
    # Wait GENERICALLY for the browser WebRTC join — no sip.* attribute reads. Bound the
    # wait by answer_timeout_s (mirrors the outbound no-answer backstop): a test where the
    # browser never connects must not pin a worker slot until LiveKit reaps the room
    # (security review PR #61, LOW #3).
    try:
        await asyncio.wait_for(ctx.wait_for_participant(), timeout=cfg.timing.answer_timeout_s)
    except TimeoutError:
        # asyncio.TimeoutError is an alias of builtin TimeoutError on 3.11+.
        log.info("No browser participant within answer timeout; ending test job")
        ctx.shutdown(reason="test_no_participant")
        return
    # Arm the max-duration guard as the only bound on the test (no answer-timeout path,
    # no recording, no voicemail detection on a sandbox session).
    _guard_task = asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
    _BACKGROUND_TASKS.add(_guard_task)
    _guard_task.add_done_callback(_BACKGROUND_TASKS.discard)
    log.info("Test participant present; beginning conversation")
    await session.generate_reply(instructions=cfg.prompts.checkin_flow_instructions)


async def _handle_crisis(
    session: Any, call_id: str, settings: Settings, category: CrisisCategory
) -> None:
    """Escalate a deterministically-detected crisis and speak the emergency resource.

    Best-effort: a failed escalation still speaks a safe fallback so the contact always
    hears help. The server-side raise_crisis records the urgent flag and notifies family.
    """
    script: str | None = None
    try:
        resp = await api_client.raise_crisis(
            call_id, settings, category=category, detection_source="safety_net"
        )
        if isinstance(resp, dict):
            spoken = resp.get("spoken_script")
            script = spoken if isinstance(spoken, str) and spoken else None
    except Exception:
        logger.bind(call_id=call_id).warning("Crisis safety-net escalation failed")
    spoken_text = script or (
        "I'm very concerned about your safety. If this is an emergency, please call 911 right now."
    )
    try:
        await session.say(spoken_text, allow_interruptions=False)
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to speak crisis resource")


def _arm_crisis_safety_net(session: Any, *, call_id: str, settings: Settings) -> None:
    """Subscribe a deterministic CrisisWatcher to STT and escalate on the first match.

    The life-safety net (FR-002): it fires server-side escalation + spoken resource even
    if the LLM never calls raise_crisis. ``feed`` is sync (runs in the event handler); the
    async escalation is spawned and strong-referenced so it is not GC'd mid-flight.
    """
    watcher = CrisisWatcher()

    def _on_transcript(ev: Any) -> None:
        category = watcher.feed(ev.transcript)
        if category is None:
            return
        task = asyncio.create_task(_handle_crisis(session, call_id, settings, category))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    session.on("user_input_transcribed", _on_transcript)


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    meta = parse_metadata(ctx.job.metadata)
    log = logger.bind(room=ctx.room.name, call_id=meta.call_id, direction=meta.direction)
    log.info("Job assigned, connecting to room")

    await ctx.connect()
    log.info("Connected to room")

    # Pre-publish Test Audio: a fully sandboxed branch that never resolves a published
    # config, never looks up an inbound caller, and never writes a production record.
    if meta.session_kind == "test":
        await _run_test_session(ctx, settings, meta)
        return

    # Resolve the published agent config once per call (best-effort; never raises).
    # meta.direction is a free-form str from dispatch metadata; narrow it to the
    # config endpoint's Literal (anything other than "outbound" is treated as inbound).
    direction: Literal["inbound", "outbound"] = (
        "outbound" if meta.direction == "outbound" else "inbound"
    )
    cfg = await fetch_agent_config(settings, direction=direction, call_id=meta.call_id)

    if meta.direction == "outbound" and meta.call_id:
        # call_id comes from job-dispatch metadata (a less-trusted boundary) and flows
        # into URL paths and the GCS recording key. Validate once here and use the
        # checked value downstream; a malformed id means a bad dispatch — drop the job.
        try:
            call_id = validate_call_id(meta.call_id)
        except ValueError:
            log.error("Invalid call_id in job metadata; refusing outbound job")
            ctx.shutdown(reason="invalid_metadata")
            return
        data = CheckInData(
            call_id=call_id,
            settings=settings,
            job_ctx=ctx,
            goodbye_message=cfg.prompts.goodbye_message,
        )
        session = build_session(settings, cfg, userdata=data)
        agent = build_check_in_agent(
            cfg,
            resolved_vars=meta.resolved_vars,
            custom_vars=meta.dynamic_vars,
            timezone=meta.timezone,
        )
        register_transcript_flush(ctx, session, call_id, settings)
        register_metrics_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        log.info("Session started; waiting for participant")
        try:
            # answer_timeout_s is driven by agent config (cfg.timing.answer_timeout_s);
            # the former settings.outbound_answer_timeout_s field was removed in favor
            # of config-driven timing (see agent_config.TimingConfig).
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=cfg.timing.answer_timeout_s)
        except TimeoutError:
            # asyncio.TimeoutError is an alias of builtin TimeoutError on 3.11+.
            # The API's dial task classifies/cleans up no-answer; this is the
            # agent-side backstop so the job never hangs on an unanswered call.
            log.info("No participant within answer timeout; ending job")
            ctx.shutdown(reason="no_answer_timeout")
            return
        # Participant confirmed present: arm the max-duration guard now (not during the
        # answer-wait) so its clock starts on a live call and never fires on a job that
        # already shut down via the no-answer path. Hold a reference so the task is not
        # garbage-collected before completion (asyncio does not keep its own ref).
        _guard_task = asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
        _BACKGROUND_TASKS.add(_guard_task)
        _guard_task.add_done_callback(_BACKGROUND_TASKS.discard)
        # Consent before capture: speak the disclosure to completion, then start
        # egress, so no audio is recorded before the spoken notice (consent ordering).
        await say_recording_disclosure(session, cfg)
        await start_call_recording(ctx, call_id, settings)
        # Honour admin-configured voicemail phrases; empty -> built-in §7 patterns.
        watcher = VoicemailWatcher(matcher=build_matcher(cfg.voicemail_detection.trigger_phrases))
        session.on("user_input_transcribed", lambda ev: watcher.feed(ev.transcript))
        # Deterministic crisis safety net for the whole call (US1 / FR-002): escalates +
        # speaks the resource even if the LLM misses the crisis.
        _arm_crisis_safety_net(session, call_id=call_id, settings=settings)
        log.info("Participant present; running voicemail detection window")
        await _run_detection_window(
            ctx, session, watcher, call_id=call_id, settings=settings, cfg=cfg
        )
        return

    # Inbound: caller already dialed in; no voicemail detection (spec §7).
    await _run_inbound(ctx, settings, cfg, log)


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
