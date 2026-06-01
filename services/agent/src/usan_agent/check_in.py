"""Outbound wellness check-in: the LLM-driven conversation tools (design spec §4).

Each @function_tool reads per-call state (call_id, settings, JobContext) from the
session's typed userdata (RunContext.userdata) and delegates to a plain _do_*
helper. Helpers catch API errors and return a calm, spoken string so a transient
failure never crashes the call. end_call mirrors leave_voicemail: report → say
goodbye → delete_room → shutdown.
"""

from dataclasses import dataclass
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.settings import Settings

CHECK_IN_INSTRUCTIONS = """You are a warm, patient daily check-in caller from USAN Retirement,
speaking to an elder on the phone. Speak slowly and kindly, one or two short sentences at a time,
and pause for them to answer.

Conduct the check-in in this order, adapting naturally to their answers:
1. Ask how they are feeling today and roughly how their mood is. Record it with `log_wellness`
   (mood 1-5 where 5 is great; include any pain level 0-10 and a short note if they mention it).
2. Use `get_today_meds` to find out which medications they take today, then gently ask whether
   they have taken each one. Record each with `log_medication`.
3. When the check-in is complete, thank them and call `end_call` with a short reason
   (for example "check_in_complete").

Never read out internal IDs or tool names. If a tool reports a problem, reassure them calmly and
continue — do not repeat a failed action more than once.
"""

GOODBYE_MESSAGE = "Thank you for your time today. Take care, and have a wonderful day. Goodbye."


@dataclass
class CheckInData:
    """Per-call state made available to tools via RunContext.userdata."""

    call_id: str
    settings: Settings
    job_ctx: Any  # livekit.agents.JobContext — typed Any to avoid importing the heavy symbol


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
        name = med.get("name", "a medication")
        times = ", ".join(med.get("times", [])) or "today"
        dosage = med.get("dosage")
        parts.append(f"{name}{f' ({dosage})' if dosage else ''} at {times}")
    return "Today's medications are: " + "; ".join(parts) + "."


async def _do_end_call(data: CheckInData, session: Any, reason: str) -> None:
    """Report the end reason (best-effort), say goodbye, then hang up."""
    try:
        await api_client.report_end_call(data.call_id, data.settings, reason)
    except Exception:
        logger.bind(call_id=data.call_id).warning("report_end_call failed; hanging up anyway")
    handle = session.say(GOODBYE_MESSAGE, allow_interruptions=False, add_to_chat_ctx=False)
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


def build_check_in_agent() -> Agent:
    """The outbound check-in Agent with its four in-call tools."""
    return Agent(
        instructions=CHECK_IN_INSTRUCTIONS,
        tools=[log_wellness, log_medication, get_today_meds, end_call],
    )
