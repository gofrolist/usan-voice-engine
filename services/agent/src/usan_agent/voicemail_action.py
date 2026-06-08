"""The voicemail hangup sequence, extracted for unit testing.

cancel in-flight speech/LLM → speak the scripted leave-message → report the
outcome to the API → delete the room (hangs up the SIP leg) → shut the job down.
"""

from typing import Any

from loguru import logger

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.api_client import report_voicemail_left
from usan_agent.settings import Settings


async def leave_voicemail(
    ctx: Any,
    session: Any,
    call_id: str | None,
    settings: Settings,
    voicemail_message: str = DEFAULT_AGENT_CONFIG.prompts.voicemail_message,
) -> None:
    log = logger.bind(call_id=call_id)
    log.info("Voicemail detected; leaving scripted message")

    session.interrupt(force=True)  # cancel the greeting / any in-flight reply
    handle = session.say(voicemail_message, allow_interruptions=False, add_to_chat_ctx=False)
    await handle  # wait for full playout before hanging up

    if call_id:
        await report_voicemail_left(call_id, settings)

    await ctx.delete_room()  # disconnects the SIP/PSTN leg = hangup
    ctx.shutdown(reason="voicemail_left")
