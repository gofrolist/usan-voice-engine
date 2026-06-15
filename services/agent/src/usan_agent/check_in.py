"""Outbound wellness check-in: the LLM-driven conversation tools (design spec §4).

Each @function_tool reads per-call state (call_id, settings, JobContext) from the
session's typed userdata (RunContext.userdata) and delegates to a plain _do_*
helper. Helpers catch API errors and return a calm, spoken string so a transient
failure never crashes the call. end_call mirrors leave_voicemail: report → say
goodbye → delete_room → shutdown.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from livekit.agents import Agent, RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.api_client import (
    ActivityKind,
    CrisisCategory,
    FactCategory,
    FlagCategory,
    FlagSeverity,
)
from usan_agent.prompt_vars import build_vars, substitute
from usan_agent.sanitize import sanitize_prompt_value
from usan_agent.settings import Settings

# Medication field length caps — used by _do_get_today_meds / _format_times.
# (Name/context caps for the old str.format inbound path were removed with
# _inbound_instructions; values now go through prompt_vars.build_vars which applies
# a single _INJECTED_VALUE_MAX_LEN=300 cap to all injected values.)
_MED_NAME_MAX_LEN = 80
_MED_DOSAGE_MAX_LEN = 40
_MED_TIME_MAX_LEN = 20

# FlagSeverity / FlagCategory (imported from api_client, which mirrors the API's
# FlagForFollowupRequest Literals) reach the LLM as JSON-schema enums via the
# @function_tool signature — a plain `str` would let the LLM stray off-enum, the
# API would 422, and the safety flag would be silently dropped while the LLM
# hears the success phrase.

# The API bounds FlagForFollowupRequest.reason to 1..2000 chars; values outside
# that 422 — and that 422 would be swallowed below, silently losing a safety
# flag. _do_flag_for_followup normalizes instead of failing.
_FLAG_REASON_MAX_LEN = 2000


def _sanitize_prompt_value(value: Any, *, max_len: int) -> str:
    """Backward-compat alias — delegates to ``sanitize.sanitize_prompt_value``.

    New code should import ``sanitize_prompt_value`` from ``usan_agent.sanitize``
    directly.  This alias is kept so existing tests and internal helpers that
    reference the private name continue to work without a flag-day rename.
    """
    return sanitize_prompt_value(value, max_len=max_len)


CHECK_IN_INSTRUCTIONS = DEFAULT_AGENT_CONFIG.prompts.checkin_flow_instructions
GOODBYE_MESSAGE = DEFAULT_AGENT_CONFIG.prompts.goodbye_message
INBOUND_INSTRUCTIONS_TEMPLATE = DEFAULT_AGENT_CONFIG.prompts.inbound_personalization_template


@dataclass(frozen=True)
class CheckInData:
    """Per-call state made available to tools via RunContext.userdata."""

    call_id: str
    settings: Settings
    job_ctx: Any  # livekit.agents.JobContext — typed Any to avoid importing the heavy symbol
    goodbye_message: str = GOODBYE_MESSAGE


async def _do_log_wellness(
    data: CheckInData, *, mood: int | None, pain_level: int | None, notes: str | None
) -> str:
    try:
        await api_client.log_wellness(
            data.call_id, data.settings, mood=mood, pain_level=pain_level, notes=notes
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("log_wellness tool failed")
        return "I had a little trouble saving that, but let's keep going."
    return "Thank you, I've noted how you're feeling."


async def _do_log_medication(
    data: CheckInData, *, medication_name: str, taken: bool, reported_time: str | None = None
) -> str:
    try:
        await api_client.log_medication(
            data.call_id,
            data.settings,
            medication_name=medication_name,
            taken=taken,
            reported_time=reported_time,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("log_medication tool failed")
        return "I had trouble noting that medication, but we can continue."
    return "Got it, I've recorded that."


async def _do_flag_for_followup(
    data: CheckInData, *, severity: FlagSeverity, category: FlagCategory, reason: str
) -> str:
    reason = reason.strip()[:_FLAG_REASON_MAX_LEN] or "(no reason given)"
    try:
        await api_client.flag_for_followup(
            data.call_id, data.settings, severity=severity, category=category, reason=reason
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("flag_for_followup tool failed")
        return "I've made a note of that, and I'll make sure someone follows up."
    return "Thank you. I've flagged this so someone can follow up with you."


# Universal spoken fallback when the escalation POST itself fails: a crisis must
# never end with the contact hearing nothing actionable.
_CRISIS_FALLBACK = (
    "I'm really concerned about your safety. If this is an emergency, please call 911 "
    "right now, or call or text 988 for crisis support. I'm staying right here with you."
)


async def _do_raise_crisis(
    data: CheckInData, *, category: CrisisCategory, evidence: str | None = None
) -> str:
    """Escalate an LLM-detected crisis and return the resource script to speak.

    Posts detection_source="llm" (the deterministic safety net in worker.py posts
    "safety_net"; the server upsert merges them). Returns the server's spoken_script so
    the LLM relays the exact emergency resource. On ANY failure it returns the universal
    911/988 fallback rather than raising — a life-safety path must never go silent.
    """
    try:
        resp = await api_client.raise_crisis(
            data.call_id,
            data.settings,
            category=category,
            detection_source="llm",
            evidence=evidence,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("raise_crisis tool failed")
        return _CRISIS_FALLBACK
    spoken = resp.get("spoken_script")
    if isinstance(spoken, str) and spoken.strip():
        return spoken
    return _CRISIS_FALLBACK


async def _do_schedule_callback(
    data: CheckInData,
    *,
    requested_time_text: str,
    requested_at: str | None = None,
    notes: str | None = None,
) -> str:
    try:
        await api_client.schedule_callback(
            data.call_id,
            data.settings,
            requested_time_text=requested_time_text,
            requested_at=requested_at,
            notes=notes,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("schedule_callback tool failed")
        return "I had trouble noting that callback time, but we can still continue."
    return "Of course, I've noted that you'd like a call back then."


async def _do_close_family_task(data: CheckInData, *, task_id: int | None = None) -> str:
    """Mark conveyed family message(s) delivered so they aren't repeated next call.

    ``task_id`` is optional: the agent sees only the task TEXT (the open_family_tasks
    builtin), so omitting it closes ALL of this contact's open tasks. On failure it returns
    a benign confirmation rather than raising — a bookkeeping write must not derail the call.
    """
    try:
        await api_client.close_family_task(data.call_id, data.settings, task_id=task_id)
    except Exception:
        logger.bind(call_id=data.call_id).warning("close_family_task tool failed")
        return "Thank you, I'll make sure the family's message is taken care of."
    return "Thank you, I've passed that along and marked it done."


async def _do_record_personal_fact(
    data: CheckInData,
    *,
    category: FactCategory,
    content: str,
    structured: dict[str, Any] | None = None,
) -> str:
    """Remember a durable fact the contact shared. On failure return a benign confirmation
    rather than raising — a memory write must not derail the conversation."""
    try:
        await api_client.record_personal_fact(
            data.call_id,
            data.settings,
            category=category,
            content=content,
            structured=structured,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("record_personal_fact tool failed")
        return "Thank you for sharing that — I'll remember it."
    return "Thank you for telling me — I'll remember that."


# Spoken fallback when get_activity can't reach the API: a low-mood moment must still get a
# calming activity rather than silence.
_ACTIVITY_FALLBACK = (
    "Let's take a slow, gentle breath together — breathe in for four, hold gently, and "
    "breathe out slowly. We'll do that a couple of times, nice and easy."
)


async def _do_record_survey(
    data: CheckInData,
    *,
    loneliness: int | None = None,
    mood: int | None = None,
    satisfaction: int | None = None,
) -> str:
    """Record the contact's monthly wellbeing survey. On failure return a benign confirmation
    rather than raising — a bookkeeping write must not derail a gentle conversation."""
    try:
        await api_client.record_survey(
            data.call_id,
            data.settings,
            loneliness=loneliness,
            mood=mood,
            satisfaction=satisfaction,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("record_survey tool failed")
        return "Thank you for sharing how you're doing this month."
    return "Thank you, I've noted your check-in for this month."


async def _do_get_activity(data: CheckInData, *, kind: ActivityKind = "any") -> str:
    """Fetch a mood-boosting activity and return its script for the agent to guide. On any
    failure return a calming breathing fallback — a low-mood moment must not go silent."""
    try:
        resp = await api_client.get_activity(data.call_id, data.settings, kind=kind)
    except Exception:
        logger.bind(call_id=data.call_id).warning("get_activity tool failed")
        return _ACTIVITY_FALLBACK
    script = resp.get("script")
    if isinstance(script, str) and script.strip():
        return script
    return _ACTIVITY_FALLBACK


async def _do_get_today_meds(data: CheckInData) -> str:
    try:
        meds = await api_client.get_today_meds(data.call_id, data.settings)
    except Exception:
        logger.bind(call_id=data.call_id).warning("get_today_meds tool failed")
        return "I couldn't look up the medication list right now."
    if not meds:
        return "There are no medications scheduled for today."
    parts = []
    for med in meds:
        if not isinstance(med, dict):
            continue
        # These fields come from the API and are spoken back to the LLM as a tool
        # result, so sanitize them like caller dynamic_vars — defense-in-depth against
        # a poisoned/compromised upstream embedding instructions in a med name.
        raw_name = med.get("name") or "a medication"
        name = _sanitize_prompt_value(raw_name, max_len=_MED_NAME_MAX_LEN) or "a medication"
        times = _format_times(med.get("times")) or "today"
        dosage_raw = med.get("dosage")
        dosage_str = (
            f" ({_sanitize_prompt_value(dosage_raw, max_len=_MED_DOSAGE_MAX_LEN)})"
            if dosage_raw
            else ""
        )
        parts.append(f"{name}{dosage_str} at {times}")
    if not parts:
        return "There are no medications scheduled for today."
    return "Today's medications are: " + "; ".join(parts) + "."


def _format_times(times: Any) -> str:
    """Join a medication's scheduled times defensively.

    The API payload is trusted-but-validated: a non-list ``times`` (or one holding
    non-string entries) must never crash the call or produce garbled speech.
    """
    if not isinstance(times, list):
        return ""
    cleaned = (_sanitize_prompt_value(t, max_len=_MED_TIME_MAX_LEN) for t in times if t)
    return ", ".join(seg for seg in cleaned if seg)


async def _do_send_sms(data: CheckInData, *, template_key: str) -> str:
    try:
        await api_client.send_sms(data.call_id, data.settings, template_key=template_key)
    except Exception:
        logger.bind(call_id=data.call_id).warning("send_sms tool failed")
        return "I wasn't able to send that text just now, but we can continue."
    return "I've sent that text message for you."


async def _do_send_info_sms(data: CheckInData) -> str:
    """Text the contact the helpful-numbers SMS (US7 / FR-041). On failure return a calm
    confirmation rather than raising — a convenience text must not derail the call."""
    try:
        await api_client.send_info_sms(data.call_id, data.settings)
    except Exception:
        logger.bind(call_id=data.call_id).warning("send_info_sms tool failed")
        return "I wasn't able to send that text just now, but we can continue."
    return "I've texted you those helpful phone numbers."


async def _do_register_opt_out(data: CheckInData) -> str:
    """Record a spoken opt-out (US7 / FR-037). Still acknowledge warmly on failure, but log
    it — the API call is the compliance action and a failure needs human follow-up."""
    try:
        await api_client.register_opt_out(data.call_id, data.settings)
    except Exception:
        logger.bind(call_id=data.call_id).warning("register_opt_out tool failed")
        return "I understand — I'll make sure we stop the calls. Take care."
    return "Of course — I've taken you off our call list. You won't get these calls anymore."


async def _do_set_spanish_callback(data: CheckInData) -> str:
    """Record a Spanish preference + schedule a Spanish callback (US8 / FR-040). Acknowledge
    warmly on failure but log it — the callback is the action a human may need to follow up."""
    try:
        await api_client.set_spanish_callback(data.call_id, data.settings)
    except Exception:
        logger.bind(call_id=data.call_id).warning("set_spanish_callback tool failed")
        return "Of course — someone will call you back in Spanish soon."
    return "Of course — we'll call you back in Spanish soon. Take care."


async def _hang_up(data: CheckInData, session: Any) -> None:
    """Say goodbye and tear the room down. Makes NO api_client / network call.

    Shared by the live ``_do_end_call`` (which reports the end reason first) and the
    test-mode ``noop_end_call`` (which must hang up without any ``/v1/tools/*`` call).
    """
    handle = session.say(data.goodbye_message, allow_interruptions=False, add_to_chat_ctx=False)
    await handle
    await data.job_ctx.delete_room()
    data.job_ctx.shutdown(reason="ended_by_agent")


async def _do_end_call(data: CheckInData, session: Any, reason: str) -> None:
    """Report the end reason (best-effort), say goodbye, then hang up."""
    try:
        await api_client.report_end_call(data.call_id, data.settings, reason)
    except Exception:
        logger.bind(call_id=data.call_id).warning("report_end_call failed; hanging up anyway")
    await _hang_up(data, session)


@function_tool
async def log_wellness(
    ctx: RunContext[CheckInData],
    mood: int | None = None,
    pain_level: int | None = None,
    notes: str | None = None,
) -> str:
    """Record the contact's wellness this call.

    Args:
        mood: Overall mood, 1 (low) to 5 (great).
        pain_level: Pain level, 0 (none) to 10 (severe).
        notes: A short free-text note about how they are doing.
    """
    return await _do_log_wellness(ctx.userdata, mood=mood, pain_level=pain_level, notes=notes)


@function_tool
async def log_medication(
    ctx: RunContext[CheckInData],
    medication_name: str,
    taken: bool,
    reported_time: str | None = None,
) -> str:
    """Record whether the contact has taken a medication.

    Args:
        medication_name: The medication's name.
        taken: True if they have taken it, False if not.
        reported_time: Optional ISO-8601 time they said they took it.
    """
    return await _do_log_medication(
        ctx.userdata, medication_name=medication_name, taken=taken, reported_time=reported_time
    )


@function_tool
async def get_today_meds(ctx: RunContext[CheckInData]) -> str:
    """List the medications the contact is scheduled to take today."""
    return await _do_get_today_meds(ctx.userdata)


@function_tool
async def flag_for_followup(
    ctx: RunContext[CheckInData],
    severity: FlagSeverity,
    category: FlagCategory,
    reason: str,
) -> str:
    """Flag this call for a human to follow up on.

    Args:
        severity: "routine" for a non-urgent note, "urgent" for prompt attention.
        category: One of "medical", "emotional", "medication", "safety", "other".
        reason: A short description of what should be followed up on.
    """
    return await _do_flag_for_followup(
        ctx.userdata, severity=severity, category=category, reason=reason
    )


@function_tool
async def raise_crisis(
    ctx: RunContext[CheckInData],
    category: CrisisCategory,
    evidence: str | None = None,
) -> str:
    """Escalate an urgent, life-safety crisis and get the emergency resource to share.

    Call this the MOMENT the contact expresses any of the following, then calmly relay
    the spoken response and stay with them:
      - "suicidal": thoughts of suicide or self-harm
      - "medical": a medical emergency (chest pain, can't breathe, a bad fall, stroke signs)
      - "abuse": being harmed, threatened, or exploited by a caregiver or family member
      - "confusion": dangerous disorientation (doesn't know where they are, wandering)
      - "overdose": a poisoning or a medication overdose

    Args:
        category: Which crisis this is (one of the five values above).
        evidence: A short, factual phrase of what they said that prompted this.
    """
    return await _do_raise_crisis(ctx.userdata, category=category, evidence=evidence)


@function_tool
async def schedule_callback(
    ctx: RunContext[CheckInData],
    requested_time_text: str,
    requested_at: str | None = None,
    notes: str | None = None,
) -> str:
    """Record that the contact would like a call back at a particular time.

    This does not place a call; it stores a request for a human to action.

    Args:
        requested_time_text: The contact's own words for when they'd like the call back.
        requested_at: Optional best-effort ISO-8601 timestamp; omit if you can't resolve one.
        notes: Optional short free-text note about the request.
    """
    return await _do_schedule_callback(
        ctx.userdata,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
    )


@function_tool
async def close_family_task(
    ctx: RunContext[CheckInData],
    task_id: int | None = None,
) -> str:
    """Mark a family member's message as delivered once you've conveyed it.

    Call this after you have passed the family's open message(s) on to the contact so they
    are not repeated on the next call. You normally do not need a task id — omit it to
    mark all the conveyed family messages delivered.

    Args:
        task_id: Optional id of a single family task. Omit to mark all delivered.
    """
    return await _do_close_family_task(ctx.userdata, task_id=task_id)


@function_tool
async def record_personal_fact(
    ctx: RunContext[CheckInData],
    category: FactCategory,
    content: str,
    date: str | None = None,
) -> str:
    """Remember a durable fact the contact shares, to use naturally on future calls.

    Use this when they mention something lasting worth remembering — a loved one, a
    daily routine, a preference, an important date, or relevant health context. Do not
    record fleeting small talk.

    Args:
        category: One of "person", "routine", "preference", "important_date",
            "health_context".
        content: A short natural-language statement of the fact (e.g. "daughter Maria
            visits on Sundays").
        date: For an "important_date", the date in ISO YYYY-MM-DD form (e.g.
            "2026-07-04") so it can be brought up warmly when it comes around. Omit for
            other categories.
    """
    structured = {"date": date} if date else None
    return await _do_record_personal_fact(
        ctx.userdata, category=category, content=content, structured=structured
    )


@function_tool
async def record_survey(
    ctx: RunContext[CheckInData],
    loneliness: int | None = None,
    mood: int | None = None,
    satisfaction: int | None = None,
) -> str:
    """Record the contact's short monthly wellbeing survey.

    Use this only when the monthly survey is due. Ask gently and conversationally, then
    record whatever they answer (it's fine to omit a value they'd rather not give).

    Args:
        loneliness: How connected they feel, 1 (very lonely) to 5 (very connected).
        mood: Overall mood, 1 (low) to 5 (great).
        satisfaction: Satisfaction with daily life lately, 1 (low) to 5 (high).
    """
    return await _do_record_survey(
        ctx.userdata, loneliness=loneliness, mood=mood, satisfaction=satisfaction
    )


@function_tool
async def get_activity(ctx: RunContext[CheckInData], kind: ActivityKind = "any") -> str:
    """Get a short mood-boosting activity to offer when the contact's mood is low.

    Returns a gentle script (a breathing exercise, a memory exercise, or a light game) for
    you to guide them through warmly. It will not repeat one used recently. Offer it kindly
    and accept gracefully if they'd rather not.

    Args:
        kind: Which kind to fetch — "any" (default), "breathing", "memory", or "game".
    """
    return await _do_get_activity(ctx.userdata, kind=kind)


@function_tool
async def send_sms(ctx: RunContext[CheckInData], template_key: str) -> str:
    """Send the contact a pre-approved text message.

    Args:
        template_key: The id of the message template to send (choose from the
            available templates; you cannot write custom text).
    """
    return await _do_send_sms(ctx.userdata, template_key=template_key)


@function_tool
async def send_info_sms(ctx: RunContext[CheckInData]) -> str:
    """Text the contact a short list of helpful emergency and helpline phone numbers.

    Use this when the contact asks for the helpful numbers (or to have them by text). It
    sends a fixed, pre-approved message — you do not write the text yourself.
    """
    return await _do_send_info_sms(ctx.userdata)


@function_tool
async def register_opt_out(ctx: RunContext[CheckInData]) -> str:
    """Record that the contact no longer wants to be called, and stop future calls.

    Use this when the contact clearly asks not to be called anymore (or to be taken off the
    list). Acknowledge warmly; this adds their number to the do-not-call list.
    """
    return await _do_register_opt_out(ctx.userdata)


@function_tool
async def set_spanish_callback(ctx: RunContext[CheckInData]) -> str:
    """Promise the contact a Spanish-language callback and arrange it.

    Use this when the contact is speaking Spanish or asks to be helped in Spanish. Do not
    switch languages mid-call; warmly promise a callback in Spanish. This records their
    language preference and schedules the Spanish callback.
    """
    return await _do_set_spanish_callback(ctx.userdata)


@function_tool
async def end_call(ctx: RunContext[CheckInData], reason: str = "check_in_complete") -> str:
    """End the call once the check-in is complete.

    Args:
        reason: A short reason, e.g. "check_in_complete".
    """
    await _do_end_call(ctx.userdata, ctx.session, reason)
    return ""


# name -> tool callable. Full parity with the admin schema's TOOL_NAMES: every catalog
# tool (including flag_for_followup / schedule_callback / send_sms) has a landed
# @function_tool callable here. send_sms is still gated by the template guard in
# _select_tools (it is dropped unless an SMS template is configured).
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "flag_for_followup": flag_for_followup,
    "raise_crisis": raise_crisis,
    "schedule_callback": schedule_callback,
    "close_family_task": close_family_task,
    "record_personal_fact": record_personal_fact,
    "record_survey": record_survey,
    "get_activity": get_activity,
    "send_sms": send_sms,
    "send_info_sms": send_info_sms,
    "register_opt_out": register_opt_out,
    "set_spanish_callback": set_spanish_callback,
    "end_call": end_call,
}


# --- Sandboxed test-mode tools (US5 / FR-027) -------------------------------
# When a job is dispatched with session_kind=="test" (a pre-publish Test Audio run),
# the agent MUST NOT touch the database or PHI: these stubs mirror the live tools'
# names + docstrings (so the LLM sees the same tool surface) but return a canned
# string and NEVER call api_client / POST to /v1/tools/*. This is the ONLY tool
# registry reachable in test mode. noop_end_call hangs up via _hang_up (say goodbye +
# delete_room + shutdown) WITHOUT calling api_client.report_end_call — there is no Call
# row to report an end reason for, so test mode makes no /v1/tools/* request at all.


@function_tool
async def noop_log_wellness(
    ctx: RunContext[CheckInData],
    mood: int | None = None,
    pain_level: int | None = None,
    notes: str | None = None,
) -> str:
    """Record the contact's wellness this call.

    Args:
        mood: Overall mood, 1 (low) to 5 (great).
        pain_level: Pain level, 0 (none) to 10 (severe).
        notes: A short free-text note about how they are doing.
    """
    return "Thank you, I've noted how you're feeling."


@function_tool
async def noop_log_medication(
    ctx: RunContext[CheckInData],
    medication_name: str,
    taken: bool,
    reported_time: str | None = None,
) -> str:
    """Record whether the contact has taken a medication.

    Args:
        medication_name: The medication's name.
        taken: True if they have taken it, False if not.
        reported_time: Optional ISO-8601 time they said they took it.
    """
    return "Got it, I've recorded that."


@function_tool
async def noop_get_today_meds(ctx: RunContext[CheckInData]) -> str:
    """List the medications the contact is scheduled to take today."""
    return "Today's medications are: (test mode) Example Medication at 9:00 AM."


@function_tool
async def noop_flag_for_followup(
    ctx: RunContext[CheckInData],
    severity: FlagSeverity,
    category: FlagCategory,
    reason: str,
) -> str:
    """Flag this call for a human to follow up on.

    Args:
        severity: "routine" for a non-urgent note, "urgent" for prompt attention.
        category: One of "medical", "emotional", "medication", "safety", "other".
        reason: A short description of what should be followed up on.
    """
    return "Thank you. I've flagged this so someone can follow up with you."


@function_tool
async def noop_raise_crisis(
    ctx: RunContext[CheckInData],
    category: CrisisCategory,
    evidence: str | None = None,
) -> str:
    """Escalate an urgent, life-safety crisis and get the emergency resource to share.

    Call this the MOMENT the contact expresses any of the following, then calmly relay
    the spoken response and stay with them:
      - "suicidal": thoughts of suicide or self-harm
      - "medical": a medical emergency (chest pain, can't breathe, a bad fall, stroke signs)
      - "abuse": being harmed, threatened, or exploited by a caregiver or family member
      - "confusion": dangerous disorientation (doesn't know where they are, wandering)
      - "overdose": a poisoning or a medication overdose

    Args:
        category: Which crisis this is (one of the five values above).
        evidence: A short, factual phrase of what they said that prompted this.
    """
    # Sandbox mode writes nothing, but a crisis simulation must still surface a safe
    # spoken directive so a test run reflects the real call's behavior.
    return _CRISIS_FALLBACK


@function_tool
async def noop_schedule_callback(
    ctx: RunContext[CheckInData],
    requested_time_text: str,
    requested_at: str | None = None,
    notes: str | None = None,
) -> str:
    """Record that the contact would like a call back at a particular time.

    Args:
        requested_time_text: The contact's own words for when they'd like the call back.
        requested_at: Optional best-effort ISO-8601 timestamp; omit if you can't resolve one.
        notes: Optional short free-text note about the request.
    """
    return "Of course, I've noted that you'd like a call back then."


@function_tool
async def noop_close_family_task(
    ctx: RunContext[CheckInData],
    task_id: int | None = None,
) -> str:
    """Mark a family member's message as delivered once you've conveyed it.

    Call this after you have passed the family's open message(s) on to the contact so they
    are not repeated on the next call. You normally do not need a task id — omit it to
    mark all the conveyed family messages delivered.

    Args:
        task_id: Optional id of a single family task. Omit to mark all delivered.
    """
    return "Thank you, I've passed that along and marked it done."


@function_tool
async def noop_record_personal_fact(
    ctx: RunContext[CheckInData],
    category: FactCategory,
    content: str,
    date: str | None = None,
) -> str:
    """Remember a durable fact the contact shares, to use naturally on future calls.

    Use this when they mention something lasting worth remembering — a loved one, a
    daily routine, a preference, an important date, or relevant health context. Do not
    record fleeting small talk.

    Args:
        category: One of "person", "routine", "preference", "important_date",
            "health_context".
        content: A short natural-language statement of the fact (e.g. "daughter Maria
            visits on Sundays").
        date: For an "important_date", the date in ISO YYYY-MM-DD form (e.g.
            "2026-07-04") so it can be brought up warmly when it comes around. Omit for
            other categories.
    """
    return "Thank you for telling me — I'll remember that."


@function_tool
async def noop_record_survey(
    ctx: RunContext[CheckInData],
    loneliness: int | None = None,
    mood: int | None = None,
    satisfaction: int | None = None,
) -> str:
    """Record the contact's short monthly wellbeing survey.

    Use this only when the monthly survey is due. Ask gently and conversationally, then
    record whatever they answer (it's fine to omit a value they'd rather not give).

    Args:
        loneliness: How connected they feel, 1 (very lonely) to 5 (very connected).
        mood: Overall mood, 1 (low) to 5 (great).
        satisfaction: Satisfaction with daily life lately, 1 (low) to 5 (high).
    """
    return "Thank you, I've noted your check-in for this month."


@function_tool
async def noop_get_activity(ctx: RunContext[CheckInData], kind: ActivityKind = "any") -> str:
    """Get a short mood-boosting activity to offer when the contact's mood is low.

    Returns a gentle script (a breathing exercise, a memory exercise, or a light game) for
    you to guide them through warmly. It will not repeat one used recently. Offer it kindly
    and accept gracefully if they'd rather not.

    Args:
        kind: Which kind to fetch — "any" (default), "breathing", "memory", or "game".
    """
    # Sandbox mode writes nothing, but still returns a real, usable activity script so a
    # test run reflects the live low-mood flow.
    return _ACTIVITY_FALLBACK


@function_tool
async def noop_send_sms(ctx: RunContext[CheckInData], template_key: str) -> str:
    """Send the contact a pre-approved text message.

    Args:
        template_key: The id of the message template to send (choose from the
            available templates; you cannot write custom text).
    """
    return "I've sent that text message for you."


@function_tool
async def noop_send_info_sms(ctx: RunContext[CheckInData]) -> str:
    """Text the contact a short list of helpful emergency and helpline phone numbers.

    Use this when the contact asks for the helpful numbers (or to have them by text). It
    sends a fixed, pre-approved message — you do not write the text yourself.
    """
    return "I've texted you those helpful phone numbers."


@function_tool
async def noop_register_opt_out(ctx: RunContext[CheckInData]) -> str:
    """Record that the contact no longer wants to be called, and stop future calls.

    Use this when the contact clearly asks not to be called anymore (or to be taken off the
    list). Acknowledge warmly; this adds their number to the do-not-call list.
    """
    return "Of course — I've taken you off our call list. You won't get these calls anymore."


@function_tool
async def noop_set_spanish_callback(ctx: RunContext[CheckInData]) -> str:
    """Promise the contact a Spanish-language callback and arrange it.

    Use this when the contact is speaking Spanish or asks to be helped in Spanish. Do not
    switch languages mid-call; warmly promise a callback in Spanish. This records their
    language preference and schedules the Spanish callback.
    """
    return "Of course — we'll call you back in Spanish soon. Take care."


@function_tool
async def noop_end_call(ctx: RunContext[CheckInData], reason: str = "check_in_complete") -> str:
    """End the call once the check-in is complete.

    Args:
        reason: A short reason, e.g. "check_in_complete".
    """
    # Test mode hangs up gracefully but makes NO api_client / /v1/tools/* call: there
    # is no Call row to report an end reason for (FR-027).
    await _hang_up(ctx.userdata, ctx.session)
    return ""


# No-op registry: stub callables that return canned strings and NEVER call api_client.
# Keyed by the SAME catalog names as _TOOL_REGISTRY so test mode offers the identical
# tool surface to the LLM while writing nothing.
_TEST_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": noop_log_wellness,
    "log_medication": noop_log_medication,
    "get_today_meds": noop_get_today_meds,
    "flag_for_followup": noop_flag_for_followup,
    "raise_crisis": noop_raise_crisis,
    "schedule_callback": noop_schedule_callback,
    "close_family_task": noop_close_family_task,
    "record_personal_fact": noop_record_personal_fact,
    "record_survey": noop_record_survey,
    "get_activity": noop_get_activity,
    "send_sms": noop_send_sms,
    "send_info_sms": noop_send_info_sms,
    "register_opt_out": noop_register_opt_out,
    "set_spanish_callback": noop_set_spanish_callback,
    "end_call": noop_end_call,
}


class _ToolsConfigLike(Protocol):
    """Structural view of what _select_tools requires off a ToolsConfig.

    Only ``.enabled`` is a hard requirement. ``.sms`` is deliberately NOT declared
    here even though the concrete ``ToolsConfig`` now carries it: the tests'
    ``SimpleNamespace`` objects (and any older config document) may omit it, so the
    readers use ``getattr`` and this Protocol keeps that defensive access honest.
    """

    enabled: list[str]


def _select_test_tools(tools: _ToolsConfigLike) -> list[Any]:
    """Resolve enabled tool names to the no-op TEST callables, preserving order.

    Sandbox parallel of ``_select_tools`` (FR-027): every callable comes from
    ``_TEST_TOOL_REGISTRY`` so a pre-publish Test Audio run exercises the same tool
    surface the LLM would see live, but no stub touches ``api_client``. The send_sms
    template guard and the always-include-end_call rule are preserved so the test
    behaves like a real call minus the database writes.
    """
    names = [n for n in tools.enabled if n in _TEST_TOOL_REGISTRY]  # preserve enabled order
    if not _sms_templates(tools):
        names = [n for n in names if n != "send_sms"]
    if "end_call" not in names:
        names.append("end_call")
    return [_TEST_TOOL_REGISTRY[n] for n in names]


def _select_tools(tools: _ToolsConfigLike) -> list[Any]:
    """Resolve enabled tool names to callables, preserving order.

    Any enabled name absent from _TOOL_REGISTRY is silently dropped. That covers both
    unknown names (already rejected upstream by the admin schema) and catalog tools
    whose agent-side callable has not landed yet (see _TOOL_REGISTRY note) -- enabling
    such a tool is accepted by the API but is a no-op here until the registry catches up.
    send_sms is a dead tool until at least one SMS template is configured, so it is
    dropped unless ``tools`` carries an ``sms`` config with templates (``getattr``
    because ``_ToolsConfigLike`` does not declare ``.sms``); this template guard is the
    sole gate on send_sms. end_call is always included: it drives
    report->goodbye->delete_room->shutdown, so removing it would leave a call unable
    to end gracefully.
    """
    names = [n for n in tools.enabled if n in _TOOL_REGISTRY]  # preserve enabled order
    if not _sms_templates(tools):
        names = [n for n in names if n != "send_sms"]
    if "end_call" not in names:
        names.append("end_call")
    return [_TOOL_REGISTRY[n] for n in names]


def _sms_templates(tools: _ToolsConfigLike) -> list[Any]:
    """The configured SMS templates, or [] (shared by the tool gate + prompt suffix)."""
    sms_cfg = getattr(tools, "sms", None)
    return list(getattr(sms_cfg, "templates", None) or []) if sms_cfg else []


def _sms_template_instructions(tools: _ToolsConfigLike) -> str:
    """Instruction suffix enumerating the valid send_sms template keys.

    send_sms is a statically-registered FunctionTool, so its JSON schema cannot list
    the operator-configured template keys; without this suffix the LLM can only
    guess a key (-> 404 from the API, message silently dropped). Emitted exactly
    when _select_tools offers send_sms (same enabled + templates guard). Keys are
    server-validated slugs and labels are operator-authored config — the same trust
    level as the surrounding prompt text.
    """
    templates = _sms_templates(tools)
    if not templates or "send_sms" not in tools.enabled:
        return ""
    lines = "\n".join(f'- "{t.key}": {t.label}' for t in templates)
    return (
        f"\n\nWhen sending a text message with `send_sms`, template_key must be one of:\n{lines}\n"
    )


def build_check_in_agent(
    cfg: AgentConfig | None = None,
    *,
    resolved_vars: dict[str, str] | None = None,
    custom_vars: dict[str, Any] | None = None,
    timezone: str = "",
    now: datetime | None = None,
) -> Agent:
    """The outbound check-in Agent with substituted instructions + enabled tools.

    All prompt vars (built-in + custom) are merged via build_vars and substituted
    token-scoped across the configured flow instructions. With no vars supplied the
    default token-free template renders unchanged (backward compatible).
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    values = build_vars(
        resolved_vars or {},
        custom_vars or {},
        timezone=timezone,
        now=now or datetime.now(UTC),
    )
    return Agent(
        instructions=substitute(cfg.prompts.checkin_flow_instructions, values)
        + _sms_template_instructions(cfg.tools),
        tools=_select_tools(cfg.tools),
    )


def build_inbound_agent(
    cfg: AgentConfig | None,
    *,
    resolved_vars: dict[str, str] | None = None,
    custom_vars: dict[str, Any] | None = None,
    timezone: str = "",
    now: datetime | None = None,
) -> Agent:
    """The inbound check-in Agent: configured tools + personalized instructions.

    Substitutes ``{{tokens}}`` AND the two legacy single-brace slots
    (``{contact_name}``, ``{last_check_in_line}``) across the inbound
    personalization template, so both new and already-published templates render.
    All injected values (resolved built-in + custom) are sanitized inside
    ``build_vars`` before substitution.  ``last_check_in_line`` is derived from
    ``last_check_in`` inside ``build_vars`` when not already present, so the
    logic is shared with the outbound path.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    values = build_vars(
        resolved_vars or {},
        custom_vars or {},
        timezone=timezone,
        now=now or datetime.now(UTC),
    )
    return Agent(
        instructions=substitute(cfg.prompts.inbound_personalization_template, values)
        + _sms_template_instructions(cfg.tools),
        tools=_select_tools(cfg.tools),
    )


def build_test_agent(
    cfg: AgentConfig | None = None,
    *,
    resolved_vars: dict[str, str] | None = None,
    custom_vars: dict[str, Any] | None = None,
    timezone: str = "",
    now: datetime | None = None,
) -> Agent:
    """The SANDBOXED test Agent: draft instructions + the no-op test tool registry.

    Parallel of ``build_check_in_agent`` for ``session_kind=="test"`` (FR-027): it
    substitutes the draft prompt with the admin-supplied SYNTHETIC vars exactly as a
    live call would, but every tool is a no-op stub from ``_TEST_TOOL_REGISTRY`` so
    the simulation writes nothing and calls no ``/v1/tools/*`` endpoint.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    values = build_vars(
        resolved_vars or {},
        custom_vars or {},
        timezone=timezone,
        now=now or datetime.now(UTC),
    )
    return Agent(
        instructions=substitute(cfg.prompts.checkin_flow_instructions, values)
        + _sms_template_instructions(cfg.tools),
        tools=_select_test_tools(cfg.tools),
    )
