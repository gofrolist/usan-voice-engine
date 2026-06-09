import asyncio
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
    """Raised when an outbound call cannot be dispatched (permanent misconfig)."""


class OutboundProvisioningError(Exception):
    """Raised when the outbound SIP trunk could not be resolved/created.

    Carries a sanitized message only — never the LiveKit request, which holds the
    Telnyx SIP password. Treated as a (bounded-)retryable dial failure upstream.
    """


def build_livekit_api(settings: Settings) -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_http_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


# The LiveKit SIP outbound trunk ID (ST_...) is environment-specific — it only
# exists inside a particular LiveKit instance. Rather than require operators to
# create the trunk by hand and pin its ID in an env var, we resolve it at first
# dial: reuse a trunk named ``settings.livekit_outbound_trunk_name`` if present,
# otherwise create one from the Telnyx SIP credentials, then cache the result for
# the process lifetime. An explicit LIVEKIT_SIP_OUTBOUND_TRUNK_ID still wins as
# an override. The cache is per-process; a stale entry (trunk deleted out-of-band)
# self-heals because a dial failure with no SIP status code invalidates it (see
# _dial_and_classify), so the next retry re-provisions. NOTE: with multiple API
# instances the resolve is not globally locked, so a first-ever concurrent dial
# across instances could create duplicate same-named trunks; the current deploy
# is single-instance. Pin LIVEKIT_SIP_OUTBOUND_TRUNK_ID if you scale out.
_outbound_trunk_id_cache: dict[str, str] = {}
_outbound_trunk_lock = asyncio.Lock()


def outbound_configured(settings: Settings) -> bool:
    """True when an outbound call can be placed: a caller ID plus either an
    explicit trunk-ID override or the Telnyx SIP credentials needed to
    auto-provision the trunk."""
    if not settings.telnyx_caller_id:
        return False
    if settings.livekit_sip_outbound_trunk_id:
        return True
    return bool(settings.telnyx_sip_username and settings.telnyx_sip_password)


def invalidate_outbound_trunk_cache(settings: Settings) -> None:
    """Drop the cached trunk ID so the next resolve re-lists/re-provisions."""
    _outbound_trunk_id_cache.pop(settings.livekit_outbound_trunk_name, None)


async def resolve_outbound_trunk_id(settings: Settings) -> str:
    """Return the LiveKit SIP outbound trunk ID, provisioning it if needed.

    Uses the explicit override when set; otherwise finds a trunk named
    ``settings.livekit_outbound_trunk_name`` (creating it from the Telnyx SIP
    credentials when absent) and caches the result for the process lifetime.
    Raises ``OutboundDispatchError`` for a permanent misconfig (missing creds)
    and ``OutboundProvisioningError`` (sanitized) if the LiveKit call fails.
    """
    if settings.livekit_sip_outbound_trunk_id:
        return settings.livekit_sip_outbound_trunk_id

    name = settings.livekit_outbound_trunk_name
    cached = _outbound_trunk_id_cache.get(name)
    if cached:
        return cached

    caller_id = settings.telnyx_caller_id
    sip_user = settings.telnyx_sip_username
    sip_pass = settings.telnyx_sip_password
    if not (caller_id and sip_user and sip_pass):
        raise OutboundDispatchError(
            "outbound auto-provisioning requires TELNYX_CALLER_ID, "
            "TELNYX_SIP_USERNAME and TELNYX_SIP_PASSWORD"
        )

    async with _outbound_trunk_lock:
        cached = _outbound_trunk_id_cache.get(name)
        if cached:
            return cached
        try:
            async with build_livekit_api(settings) as lkapi:
                existing = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
                for trunk in existing.items:
                    if trunk.name == name:
                        trunk_id = str(trunk.sip_trunk_id)
                        _outbound_trunk_id_cache[name] = trunk_id
                        logger.bind(trunk_id=trunk_id, name=name).info(
                            "Reusing existing outbound SIP trunk"
                        )
                        return trunk_id
                created = await lkapi.sip.create_outbound_trunk(
                    api.CreateSIPOutboundTrunkRequest(
                        trunk=api.SIPOutboundTrunkInfo(
                            name=name,
                            address=settings.telnyx_sip_host,
                            numbers=[caller_id],
                            auth_username=sip_user,
                            auth_password=sip_pass,
                        )
                    )
                )
                created_id = str(created.sip_trunk_id)
        except Exception:
            # The request object carries the SIP auth password; drop the cause so
            # it can never reach a logged traceback. Raise a sanitized error.
            logger.bind(name=name).warning("Outbound SIP trunk provisioning failed")
            raise OutboundProvisioningError("failed to provision outbound SIP trunk") from None

        _outbound_trunk_id_cache[name] = created_id
        logger.bind(trunk_id=created_id, name=name).info("Provisioned outbound SIP trunk")
        return created_id


def _outbound_metadata(call: Call, *, resolved_vars: dict[str, str] | None, timezone: str) -> str:
    # dynamic_vars stays the persisted operator/idempotency payload; the server-
    # resolved built-ins + timezone ride alongside it out-of-band (design §4.3),
    # matching the agent's CallMetadata parsing (resolved_vars, timezone).
    return json.dumps(
        {
            "call_id": str(call.id),
            "direction": "outbound",
            "dynamic_vars": call.dynamic_vars,
            "resolved_vars": resolved_vars or {},
            "timezone": timezone,
        }
    )


async def dispatch_agent(
    call: Call,
    *,
    settings: Settings,
    resolved_vars: dict[str, str] | None = None,
    timezone: str = "",
) -> None:
    """Dispatch the named agent worker into the call's room (fast, synchronous).

    ``resolved_vars``/``timezone`` carry the server-resolved built-ins to the agent
    via the dispatch metadata without persisting them (contract C, §4.3). They
    default to empty so callers that don't resolve built-ins still work.
    """
    if not outbound_configured(settings):
        raise OutboundDispatchError(
            "outbound calling not configured: set TELNYX_CALLER_ID plus Telnyx "
            "SIP credentials (TELNYX_SIP_USERNAME/TELNYX_SIP_PASSWORD), or pin "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
        )
    if not call.livekit_room:
        raise OutboundDispatchError("call has no livekit_room assigned")

    async with build_livekit_api(settings) as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=call.livekit_room,
                metadata=_outbound_metadata(call, resolved_vars=resolved_vars, timezone=timezone),
            )
        )
    logger.bind(call_id=str(call.id), room=call.livekit_room).info("Agent dispatched")


async def _create_sip_participant(call: Call, elder: Elder, settings: Settings) -> object:
    trunk_id = await resolve_outbound_trunk_id(settings)
    async with build_livekit_api(settings) as lkapi:
        return await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
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
    # is scheduled. Re-check here so a misconfigured call is marked FAILED with a
    # clear reason instead of failing deep in the dial path.
    if not outbound_configured(settings):
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
    except OutboundDispatchError:
        # Permanent misconfiguration surfaced at dial time — fail without a retry.
        async with factory() as db:
            await calls_repo.mark_dial_failure(
                db, call_id, CallStatus.FAILED, end_reason="not_configured"
            )
            await db.commit()
        await _delete_room(room, settings)
        log.info("Outbound dial failed: not_configured")
        return
    except Exception as exc:  # busy / no-answer / reject / transport / provisioning
        status, end_reason, error = classify_dial_exception(exc)
        # A failure with no SIP status code (dial_error) can mean a stale/invalid
        # cached trunk or a provisioning hiccup — drop the cache so the scheduled
        # retry re-resolves/re-provisions the trunk instead of reusing a bad ID.
        if end_reason == "dial_error":
            invalidate_outbound_trunk_cache(settings)
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
