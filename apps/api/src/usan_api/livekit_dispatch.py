import json
import uuid

from google.protobuf.duration_pb2 import Duration
from livekit import api
from loguru import logger

from usan_api.db.base import CallStatus
from usan_api.db.models import Call, Elder
from usan_api.db.session import get_session_factory
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings
from usan_api.sip_status import classify_dial_exception


class OutboundDispatchError(Exception):
    """Raised when an outbound call cannot be dispatched (misconfig)."""


def build_livekit_api(settings: Settings) -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_http_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def _outbound_metadata(call: Call) -> str:
    return json.dumps(
        {
            "call_id": str(call.id),
            "direction": "outbound",
            "dynamic_vars": call.dynamic_vars,
        }
    )


async def dispatch_agent(call: Call, *, settings: Settings) -> None:
    """Dispatch the named agent worker into the call's room (fast, synchronous)."""
    if not settings.livekit_sip_outbound_trunk_id or not settings.telnyx_caller_id:
        raise OutboundDispatchError(
            "outbound calling not configured: set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID and TELNYX_CALLER_ID"
        )
    if not call.livekit_room:
        raise OutboundDispatchError("call has no livekit_room assigned")

    async with build_livekit_api(settings) as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=call.livekit_room,
                metadata=_outbound_metadata(call),
            )
        )
    logger.bind(call_id=str(call.id), room=call.livekit_room).info("Agent dispatched")


async def _create_sip_participant(call: Call, elder: Elder, settings: Settings) -> object:
    async with build_livekit_api(settings) as lkapi:
        return await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                sip_call_to=elder.phone_e164,
                sip_number=settings.telnyx_caller_id,
                room_name=call.livekit_room,
                participant_identity="callee",
                participant_name=elder.name,
                wait_until_answered=True,
                play_ringtone=True,
                ringing_timeout=Duration(seconds=settings.outbound_ringing_timeout_s),
                max_call_duration=Duration(seconds=settings.outbound_max_call_duration_s),
            )
        )


async def _delete_room(room: str, settings: Settings) -> None:
    try:
        async with build_livekit_api(settings) as lkapi:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room))
    except Exception:  # best-effort cleanup; never mask the original dial outcome
        logger.bind(room=room).warning("delete_room failed during dial cleanup")


async def dial_and_classify(call_id: uuid.UUID, settings: Settings) -> None:
    """Background task entrypoint: dial + classify, guarded so an infra failure
    still marks the call FAILED instead of leaving it stuck at ``dialing``."""
    try:
        await _dial_and_classify(call_id, settings)
    except Exception:
        logger.bind(call_id=str(call_id)).exception("dial_and_classify crashed")
        try:
            factory = get_session_factory()
            async with factory() as db:
                failed = await calls_repo.mark_failed_if_active(
                    db, call_id, end_reason="internal_error"
                )
                if failed is not None:
                    await calls_repo.schedule_retry(db, call_id)
                await db.commit()
        except Exception:
            logger.bind(call_id=str(call_id)).warning("Could not mark call FAILED after crash")


async def _dial_and_classify(call_id: uuid.UUID, settings: Settings) -> None:
    """Dial the callee, classify the outcome, write it, clean up."""
    factory = get_session_factory()
    async with factory() as db:
        call = await calls_repo.get_call(db, call_id)
        if call is None or call.elder_id is None or not call.livekit_room:
            logger.bind(call_id=str(call_id)).warning("dial_and_classify: call not dialable")
            return
        elder = await elders_repo.get_elder(db, call.elder_id)
        if elder is None:
            return
        room = call.livekit_room

    # Belt-and-suspenders: dispatch_agent already gates on this before the dial
    # is scheduled, but the SIP request fields are typed str|None — never pass
    # None into the gRPC request; mark FAILED with a clear reason instead.
    if not settings.livekit_sip_outbound_trunk_id or not settings.telnyx_caller_id:
        async with factory() as db:
            await calls_repo.mark_dial_failure(
                db, call_id, CallStatus.FAILED, end_reason="not_configured"
            )
            await db.commit()
        await _delete_room(room, settings)
        return

    log = logger.bind(call_id=str(call_id), room=room)
    try:
        info = await _create_sip_participant(call, elder, settings)
    except Exception as exc:  # busy / no-answer / reject / transport
        status, end_reason, error = classify_dial_exception(exc)
        async with factory() as db:
            await calls_repo.mark_dial_failure(
                db, call_id, status, end_reason=end_reason, error=error
            )
            await calls_repo.schedule_retry(db, call_id)
            await db.commit()
        await _delete_room(room, settings)
        log.info(
            "Outbound dial failed: {status} ({reason})", status=status.value, reason=end_reason
        )
        return

    sip_call_id = getattr(info, "sip_call_id", None)
    async with factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id=sip_call_id)
        await db.commit()
    log.info("Outbound call answered; in_progress")


async def dispatch_and_dial(call_id: uuid.UUID, settings: Settings) -> None:
    """Poller dispatch entrypoint for a claimed retry (already flipped to DIALING).

    Re-checks DNC at dial time (the elder may have opted out since the retry was
    scheduled), dispatches the agent, then delegates to dial_and_classify. A
    permanent misconfig fails the call without a retry; any other crash marks
    FAILED and schedules a retry per §5.3.
    """
    factory = get_session_factory()
    try:
        async with factory() as db:
            call = await calls_repo.get_call(db, call_id)
            if call is None or call.elder_id is None or not call.livekit_room:
                logger.bind(call_id=str(call_id)).warning("dispatch_and_dial: call not dialable")
                return
            elder = await elders_repo.get_elder(db, call.elder_id)
            if elder is None:
                await calls_repo.mark_dial_failure(
                    db, call_id, CallStatus.FAILED, end_reason="elder_missing"
                )
                await db.commit()
                return
            room = call.livekit_room
            # DNC re-check at dial time (closes the schedule->due window).
            await dnc_repo.lock_phone(db, elder.phone_e164)
            blocked = await dnc_repo.is_blocked(db, elder.phone_e164)
            if blocked:
                await calls_repo.set_status(db, call_id, CallStatus.DNC_BLOCKED)
                await db.commit()
                logger.bind(call_id=str(call_id)).info("Retry blocked by DNC")
                await _delete_room(room, settings)
                return
            await db.commit()  # release the advisory lock before the slow dial

        try:
            await dispatch_agent(call, settings=settings)
        except OutboundDispatchError:
            async with factory() as db:
                await calls_repo.mark_dial_failure(
                    db, call_id, CallStatus.FAILED, end_reason="not_configured"
                )
                await db.commit()  # misconfig is permanent — no retry
            await _delete_room(room, settings)
            return

        await dial_and_classify(call_id, settings)
    except Exception:
        logger.bind(call_id=str(call_id)).exception("dispatch_and_dial crashed")
        try:
            async with factory() as db:
                failed = await calls_repo.mark_failed_if_active(
                    db, call_id, end_reason="internal_error"
                )
                if failed is not None:
                    await calls_repo.schedule_retry(db, call_id)
                await db.commit()
        except Exception:
            logger.bind(call_id=str(call_id)).warning("Could not mark retry FAILED after crash")
