import json

from livekit import api
from loguru import logger

from usan_api.db.models import Call, Elder
from usan_api.settings import Settings


class OutboundDispatchError(Exception):
    """Raised when an outbound call cannot be dispatched (misconfig or upstream error)."""


def build_livekit_api(settings: Settings) -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_http_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


async def dispatch_outbound_call(call: Call, *, elder: Elder, settings: Settings) -> None:
    if not settings.livekit_sip_outbound_trunk_id or not settings.telnyx_caller_id:
        raise OutboundDispatchError(
            "outbound calling not configured: set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID and TELNYX_CALLER_ID"
        )
    if not call.livekit_room:
        raise OutboundDispatchError("call has no livekit_room assigned")

    metadata = json.dumps(
        {
            "call_id": str(call.id),
            "direction": "outbound",
            "dynamic_vars": call.dynamic_vars,
        }
    )

    async with build_livekit_api(settings) as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=call.livekit_room,
                metadata=metadata,
            )
        )
        await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                sip_call_to=elder.phone_e164,
                sip_number=settings.telnyx_caller_id,
                room_name=call.livekit_room,
                participant_identity="callee",
                participant_name=elder.name,
                wait_until_answered=False,
                play_ringtone=True,
            )
        )

    logger.bind(call_id=str(call.id), room=call.livekit_room).info(
        "Dispatched agent + SIP participant for outbound call"
    )
