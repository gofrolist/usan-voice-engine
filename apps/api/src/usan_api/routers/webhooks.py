from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from livekit import api
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_webhooks, notifications, telnyx_inbound
from usan_api.compat import inbound_autocreate, sms_reply
from usan_api.db.session import get_db
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
from usan_api.phone import to_e164
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.schemas.inbound_sms import InboundSms, parse_inbound_sms
from usan_api.settings import Settings, get_settings
from usan_api.sms_outbox import flush_pending_sms
from usan_api.summarization import summarize_call

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook event strings that signal a room (and thus the call) has ended.
_ROOM_END_EVENTS = frozenset({"room_finished"})


def _recording_uri(info: Any, gcs_bucket: str | None) -> str | None:
    """The gs:// URI for a completed egress, or None if it produced no usable file."""
    if info.status != api.EgressStatus.EGRESS_COMPLETE or not info.file_results:
        return None
    object_key = info.file_results[0].filename
    if gcs_bucket and object_key:
        return f"gs://{gcs_bucket}/{object_key}"
    return info.file_results[0].location or None


@router.post("/livekit", status_code=status.HTTP_200_OK)
async def livekit_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    body = (await request.body()).decode("utf-8")
    auth = request.headers.get("Authorization", "")
    try:
        event = livekit_webhooks.verify_livekit_webhook(body, auth, settings)
    except livekit_webhooks.WebhookReplayError as exc:
        # A replayed (signature-valid but stale) delivery. Surface it as 401 — the
        # SAME status as a forged signature — so the response cannot be used as an
        # oracle distinguishing a genuine-but-stale payload from an invalid one. The
        # distinct exception type is kept only for this internal log line.
        logger.warning("Rejected replayed (stale) LiveKit webhook: {reason}", reason=str(exc))
        WEBHOOKS_TOTAL.labels(type="unknown", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
    except Exception as exc:  # invalid signature / hash mismatch / malformed
        WEBHOOKS_TOTAL.labels(type="unknown", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc

    if event.event in _ROOM_END_EVENTS and event.room and event.room.name:
        call = await calls_repo.mark_completed_if_in_progress(db, event.room.name)
        if call is not None:
            await db.commit()
            # Deliver any queued SMS after the response (own session); idempotent so the
            # end_call tool firing too is safe (design §6.3).
            background_tasks.add_task(flush_pending_sms, call.id)
            # Summarize the call for next-time memory (US4); flag-gated + idempotent per
            # call, so end_call firing this too is safe (design §memory).
            background_tasks.add_task(summarize_call, call.id)
            logger.bind(call_id=str(call.id), room=event.room.name).info(
                "Call completed via room_finished webhook"
            )
    elif event.event == "egress_started" and event.egress_info.room_name:
        info = event.egress_info
        call = await calls_repo.set_egress_id(db, info.room_name, info.egress_id)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), egress_id=info.egress_id).info(
                "Recorded egress_id via egress_started webhook"
            )
    elif event.event == "egress_ended" and event.egress_info.room_name:
        info = event.egress_info
        uri = _recording_uri(info, settings.gcs_bucket)
        if uri is None:
            failed = await calls_repo.set_recording_status(db, info.room_name, "failed")
            if failed is not None:
                await db.commit()
            logger.bind(room=info.room_name, status=int(info.status)).warning(
                "Egress ended without a usable recording"
            )
        else:
            call = await calls_repo.set_recording_uri(db, info.room_name, uri)
            if call is not None:
                await db.commit()
                logger.bind(call_id=str(call.id), has_recording=True).info(
                    "Stored recording_uri via egress_ended webhook"
                )
    WEBHOOKS_TOTAL.labels(type=event.event, outcome="ok").inc()
    return {"ok": True}


@router.post("/telnyx", status_code=status.HTTP_200_OK)
async def telnyx_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Inbound Telnyx SMS: verify Ed25519 signature, then route family-task intake (US2).

    Both a forged signature and a stale (replayed) delivery return 401 — the SAME status
    — so the response cannot be used as an oracle. A non-message event or a malformed but
    signature-valid payload is acknowledged (200) without side effects.
    """
    raw_body = await request.body()
    signature = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")
    try:
        payload = telnyx_inbound.verify_telnyx_webhook(raw_body, signature, timestamp, settings)
    except telnyx_inbound.TelnyxReplayError as exc:
        logger.warning("Rejected replayed (stale) Telnyx webhook: {reason}", reason=str(exc))
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
    except telnyx_inbound.InvalidTelnyxSignatureError as exc:
        logger.warning("Rejected Telnyx webhook (invalid signature): {reason}", reason=str(exc))
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
    except ValueError as exc:
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="invalid").inc()
        raise HTTPException(status_code=400, detail="invalid webhook body") from exc

    inbound = parse_inbound_sms(payload)
    if inbound is None:
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="ignored").inc()
        return {"ok": True}
    # Opt-out keywords (STOP/UNSUBSCRIBE/…) are handled BEFORE family-task intake so a
    # STOP is never relayed as a task (US7 / FR-038).
    if telnyx_inbound.is_opt_out_keyword(inbound.text):
        await _route_inbound_opt_out(db, inbound)
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="opt_out").inc()
        return {"ok": True}
    if await sms_reply.handle_inbound_sms_reply(db, settings, inbound):
        # The reply engine owns the message (and increments its own metric).
        return {"ok": True}
    if await inbound_autocreate.handle_inbound_autocreate(db, settings, inbound):
        # Auto-create owns an unknown-recipient inbound to a bound DID (own metric).
        return {"ok": True}
    await _route_inbound_family_task(db, inbound)
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="ok").inc()
    return {"ok": True}


async def _route_inbound_opt_out(db: AsyncSession, inbound: InboundSms) -> None:
    """Honor an inbound SMS opt-out keyword (US7 / FR-038).

    Adds the sender's number to the do-not-call list so no further outbound is placed
    (SC-010) — unconditionally, even for an unrecognized number (a STOP from anyone is
    honored). When the sender resolves to a known contact, a one-time PHI-free
    acknowledgement is enqueued (idempotent on the Telnyx message id). The number is
    canonicalized to E.164 so the DNC key matches the contact's stored number and the
    outbound gate suppresses the dial.
    """
    phone = to_e164(inbound.from_number) or inbound.from_number
    await dnc_repo.lock_phone(db, phone)
    await dnc_repo.add_entry(db, phone, "inbound SMS opt-out keyword (US7 / FR-038)")
    contact = await contacts_repo.get_contact_by_phone(db, phone)
    if contact is not None:
        await notifications.enqueue_opt_out_ack(
            db,
            contact_id=contact.id,
            to_number=phone,
            dedupe_key=f"opt_out_sms:{inbound.message_id}",
        )
    await db.commit()
    # Bind only the message id — the sender's number is PHI; the DNC row is the record.
    logger.bind(message_id=inbound.message_id).info("Inbound opt-out: number added to DNC")


async def _route_inbound_family_task(db: AsyncSession, inbound: InboundSms) -> None:
    """Create an open family task per linked contact for a known sender (FR-008/014/015).

    Unmatched senders create nothing (FR-014, safe default: ignored + logged). A task
    that conflicts with medical safety is flagged ``needs_safety_review`` so it is not
    relayed verbatim (FR-015). Idempotent on the Telnyx message id per contact.
    """
    # Canonicalize the sender to E.164 before the exact-match contact lookup: Telnyx may
    # deliver a bare national number, and family_contacts store E.164 — without this a known
    # contact texting from a 10-digit number would silently match nothing (FR-008/014).
    # Mirrors the opt-out path's normalization.
    phone = to_e164(inbound.from_number) or inbound.from_number
    contacts = await family_contacts_repo.find_contacts_by_phone(db, phone)
    if not contacts:
        logger.bind(message_id=inbound.message_id).info(
            "Inbound SMS from unmatched number; no task created (FR-014)"
        )
        return
    needs_review = telnyx_inbound.is_medically_unsafe(inbound.text)
    for contact in contacts:
        await family_tasks_repo.create_inbound_task(
            db,
            contact_id=contact.contact_id,
            family_contact_id=contact.id,
            message=inbound.text,
            # One number may relate to >1 contact, so dedupe per (message, contact) while
            # still creating a task for each linked contact.
            inbound_message_id=f"{inbound.message_id}:{contact.id}",
            needs_safety_review=needs_review,
        )
    await db.commit()
