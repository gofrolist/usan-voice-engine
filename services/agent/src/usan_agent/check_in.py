"""Outbound wellness check-in: the LLM-driven conversation tools (design spec §4).

Each @function_tool reads per-call state (call_id, settings, JobContext) from the
session's typed userdata (RunContext.userdata) and delegates to a plain _do_*
helper. Helpers catch API errors and return a calm, spoken string so a transient
failure never crashes the call. end_call mirrors leave_voicemail: report → say
goodbye → delete_room → shutdown.
"""

import re
from dataclasses import dataclass
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.settings import Settings

# Control characters and the format-slot braces are stripped from any API-supplied
# string before it reaches an LLM prompt (design spec §3 dynamic vars are caller
# data, not trusted instructions). Removing "{"/"}" closes both a prompt-injection
# vector and an str.format KeyError/IndexError on attacker-controlled slots.
_PROMPT_UNSAFE = re.compile(
    # format-slot braces; ASCII control chars; the Unicode line/paragraph separators
    # NEL (U+0085), LS (U+2028), PS (U+2029); and invisible/directional chars
    # (zero-width, bidi overrides) that could smuggle instructions or new lines past
    # the LLM. Separators are listed explicitly so the regex alone suffices and does
    # not silently rely on the later str.split() to drop them.
    r"[{}\x00-\x1f\x7f\x85\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\ufeff]"
)
_NAME_MAX_LEN = 100
_CONTEXT_MAX_LEN = 300
_MED_NAME_MAX_LEN = 80
_MED_DOSAGE_MAX_LEN = 40
_MED_TIME_MAX_LEN = 20


def _sanitize_prompt_value(value: Any, *, max_len: int) -> str:
    """Neutralize an API-supplied string for safe interpolation into LLM instructions.

    Strips format-slot braces and control characters (including newlines), collapses
    surrounding whitespace, and caps the length so a hostile value can neither inject
    new instructions nor introduce ``str.format`` slots.
    """
    text = _PROMPT_UNSAFE.sub(" ", str(value))
    text = " ".join(text.split())
    return text[:max_len].strip()


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


# name -> tool callable; mirrors the admin schema's TOOL_NAMES.
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "end_call": end_call,
}


def _select_tools(enabled: list[str]) -> list[Any]:
    """Resolve enabled tool names to callables, preserving order.

    Unknown names (already rejected by the admin schema) are dropped defensively.
    end_call is always included: it drives report->goodbye->delete_room->shutdown, so
    removing it would leave a call unable to end gracefully.
    """
    names = [n for n in enabled if n in _TOOL_REGISTRY]
    if "end_call" not in names:
        names.append("end_call")
    return [_TOOL_REGISTRY[n] for n in names]


def build_check_in_agent(cfg: AgentConfig | None = None) -> Agent:
    """The outbound check-in Agent with its configured instructions + enabled tools."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=cfg.prompts.checkin_flow_instructions,
        tools=_select_tools(cfg.tools.enabled),
    )


def _inbound_instructions(template: str, dynamic_vars: dict[str, Any]) -> str:
    """Render the inbound instructions from the resolved template, weaving in dynamic vars.

    The dynamic vars are API-supplied (ultimately caller-derived) data, so each value
    is sanitized before interpolation: it can introduce neither new format slots nor
    fresh prompt instructions. Only the two allowed slots (elder_name,
    last_check_in_line) are ever passed to .format — never admin-supplied kwargs.
    """
    elder_name = (
        _sanitize_prompt_value(
            dynamic_vars.get("elder_name") or "the caller", max_len=_NAME_MAX_LEN
        )
        or "the caller"
    )
    last_check_in = _sanitize_prompt_value(
        dynamic_vars.get("last_check_in") or "", max_len=_CONTEXT_MAX_LEN
    )
    last_check_in_line = (
        f"For context, their last check-in was {last_check_in}.\n" if last_check_in else ""
    )
    return template.format(elder_name=elder_name, last_check_in_line=last_check_in_line)


def build_inbound_agent(cfg: AgentConfig | None, dynamic_vars: dict[str, Any]) -> Agent:
    """The inbound check-in Agent: configured tools + personalized instructions."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=_inbound_instructions(
            cfg.prompts.inbound_personalization_template, dynamic_vars
        ),
        tools=_select_tools(cfg.tools.enabled),
    )
