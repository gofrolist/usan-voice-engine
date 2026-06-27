"""Inbound two-way SMS reply engine (Phase 4b-2).

A matched-and-owned inbound reply: find the open sms_chat for an inbound message, persist
the recipient turn as role="sms" with the Telnyx message id (dedup), generate one Vertex
reply via the shared chat path, persist it as role="agent", and send it back. Inert behind
telnyx_inbound_sms_reply_enabled. PHI/secret-safe: logs only message_id + type(exc).__name__.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import telnyx_messaging
from usan_api.compat.chat_service import _sms_send_ready, generate_agent_reply
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
from usan_api.phone import to_e164
from usan_api.repositories import chats as chats_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import Settings


def _count(outcome: str) -> None:
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()


async def handle_inbound_sms_reply(
    db: AsyncSession, settings: Settings, inbound: InboundSms
) -> bool:
    """Drive one agent reply turn for an inbound SMS. Returns True iff the engine OWNS the
    message (an open sms_chat matched) — in which case the caller must NOT also route it to
    family-task intake. Returns False for flag-off / empty to_number / no matching session."""
    if not settings.telnyx_inbound_sms_reply_enabled or not inbound.to_number:
        return False
    our_number = to_e164(inbound.to_number) or inbound.to_number
    recipient = to_e164(inbound.from_number) or inbound.from_number
    session = await chats_repo.find_open_sms_chat(db, our_number=our_number, recipient=recipient)
    if session is None:
        return False

    # Matched: the sender is an sms_chat participant — we own the message from here, even if
    # we cannot reply (never relay a chat participant into family-task intake).
    if not _sms_send_ready(settings) or not settings.gcp_project:
        logger.bind(message_id=inbound.message_id).warning(
            "inbound sms reply skipped: messaging/Vertex not configured"
        )
        _count("sms_reply_unconfigured")
        return True

    # Persist the inbound turn first (role="sms"); the Telnyx message id dedups redeliveries.
    try:
        seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db,
            session_id=session.id,
            seq=seq,
            role="sms",
            content=inbound.text,
            provider_message_id=inbound.message_id,
        )
        await db.flush()
    except IntegrityError:
        await db.rollback()
        _count("sms_reply_dedup")
        return True

    # Generate + persist + send the reply; ANY failure discards the whole txn (no orphan PHI).
    try:
        reply = await generate_agent_reply(db, settings, session)
        reply_seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=reply_seq, role="agent", content=reply
        )
        await db.flush()
        await telnyx_messaging.send_sms(settings, to_number=recipient, body=reply)
    except Exception as exc:
        await db.rollback()
        logger.bind(message_id=inbound.message_id, err=type(exc).__name__).error(
            "inbound sms reply failed"
        )
        _count("sms_reply_failed")
        return True

    # Commit OUTSIDE the send try: wrapping it would mislabel a commit-fail as a send-fail
    # (the reply already went out) and risk a double-send on retry.
    await db.commit()
    _count("sms_reply")
    return True
