"""Outbound wellness check-in: the LLM-driven conversation tools (design spec §4).

Each @function_tool reads per-call state (call_id, settings, JobContext) from the
session's typed userdata (RunContext.userdata) and delegates to a plain _do_*
helper. Helpers catch API errors and return a calm, spoken string so a transient
failure never crashes the call. end_call mirrors leave_voicemail: report → say
goodbye → delete_room → shutdown.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
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


async def _do_end_call(data: CheckInData, session: Any, reason: str) -> None:
    """Report the end reason (best-effort), say goodbye, then hang up."""
    try:
        await api_client.report_end_call(data.call_id, data.settings, reason)
    except Exception:
        logger.bind(call_id=data.call_id).warning("report_end_call failed; hanging up anyway")
    handle = session.say(data.goodbye_message, allow_interruptions=False, add_to_chat_ctx=False)
    await handle
    await data.job_ctx.delete_room()
    data.job_ctx.shutdown(reason="ended_by_agent")


@function_tool
async def log_wellness(
    ctx: RunContext[CheckInData],
    mood: int | None = None,
    pain_level: int | None = None,
    notes: str | None = None,
) -> str:
    """Record the elder's wellness this call.

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
    """Record whether the elder has taken a medication.

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
    """List the medications the elder is scheduled to take today."""
    return await _do_get_today_meds(ctx.userdata)


@function_tool
async def end_call(ctx: RunContext[CheckInData], reason: str = "check_in_complete") -> str:
    """End the call once the check-in is complete.

    Args:
        reason: A short reason, e.g. "check_in_complete".
    """
    await _do_end_call(ctx.userdata, ctx.session, reason)
    return ""


# name -> tool callable. A SUBSET of the admin schema's TOOL_NAMES during rollout:
# flag_for_followup / schedule_callback / send_sms are valid catalog names (so they
# validate + render in the editor) but their @function_tool callables land in Parts
# B/C/D. Until then _select_tools drops them, so enabling one saves but is a no-op.
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "end_call": end_call,
}


def _select_tools(enabled: list[str]) -> list[Any]:
    """Resolve enabled tool names to callables, preserving order.

    Any enabled name absent from _TOOL_REGISTRY is silently dropped. That covers both
    unknown names (already rejected upstream by the admin schema) and catalog tools
    whose agent-side callable has not landed yet (see _TOOL_REGISTRY note) -- enabling
    such a tool is accepted by the API but is a no-op here until the registry catches up.
    end_call is always included: it drives report->goodbye->delete_room->shutdown, so
    removing it would leave a call unable to end gracefully.
    """
    names = [n for n in enabled if n in _TOOL_REGISTRY]
    if "end_call" not in names:
        names.append("end_call")
    return [_TOOL_REGISTRY[n] for n in names]


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
        instructions=substitute(cfg.prompts.checkin_flow_instructions, values),
        tools=_select_tools(cfg.tools.enabled),
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
    (``{elder_name}``, ``{last_check_in_line}``) across the inbound
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
        instructions=substitute(cfg.prompts.inbound_personalization_template, values),
        tools=_select_tools(cfg.tools.enabled),
    )
