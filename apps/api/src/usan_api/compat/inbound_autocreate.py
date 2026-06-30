"""Unknown-recipient inbound SMS auto-create (Phase 4b-3).

When an inbound SMS arrives at a provisioned DID that carries an inbound_sms_agents
binding and matches no open sms_chat, auto-create an sms_chat oriented like an outbound
one (from=our DID, to=sender, ONGOING), persist the inbound turn role="sms" (the dedup
point), run one Vertex reply, persist it role="agent", and send it. Inert behind
telnyx_inbound_sms_autocreate_enabled. Single-org (RLS default org). PHI/secret-safe:
logs only message_id + type(exc).__name__ (never message text, reply, agent_id, or phone).
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import telnyx_messaging
from usan_api.compat import ids
from usan_api.compat.chat_service import _sms_send_ready, generate_agent_reply
from usan_api.compat.errors import CompatError
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
from usan_api.phone import to_e164
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import chats as chats_repo
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import phone_numbers as phone_numbers_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import Settings


def _count(outcome: str) -> None:
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()


def _pick_inbound_sms_agent(pn: PhoneNumber | None) -> str | None:
    """First-entry agent_id token from the DID's inbound_sms_agents binding, else None.

    Deterministic first-entry mirrors _resolve_sms_agent's outbound[0] pick (chat_service.py)
    and is isolated here so a weighted-random pick can replace it without touching the caller.
    Our schema stores only the list (the oracle's inbound_sms_agent_id scalar is collapsed
    into it by the Phase 2 CRUD), so first-entry IS the scalar-equivalent.
    """
    agents = (pn.inbound_sms_agents if pn is not None else None) or []
    token = (agents[0] or {}).get("agent_id") if agents else None
    return token if isinstance(token, str) and token else None


async def _resolve_live_profile(db: AsyncSession, token: str) -> AgentProfile | None:
    """The live published AgentProfile for a bound agent token, or None when the token is
    malformed / missing / not ACTIVE / unpublished. Channel-lenient like create_sms_chat
    (a voice agent bound to an SMS number is accepted)."""
    try:
        profile_id = ids.decode_agent_id(token)
    except CompatError:
        return None
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    if (
        profile is None
        or profile.status is not ProfileStatus.ACTIVE
        or profile.published_version is None
    ):
        return None
    return profile


async def handle_inbound_autocreate(
    db: AsyncSession, settings: Settings, inbound: InboundSms
) -> bool:
    """Auto-create an sms_chat for an inbound SMS to a bound DID and run one reply turn.

    Returns True iff the handler OWNS the message (created+replied, deduped, or
    messaging-unconfigured skip) — the caller must NOT then route it to family-task. Returns
    False when a gate declines (flag off / empty to_number / no usable binding / known family
    contact), so the family-task fall-through still runs.
    """
    if not settings.telnyx_inbound_sms_autocreate_enabled or not inbound.to_number:
        return False
    our_number = to_e164(inbound.to_number) or inbound.to_number
    recipient = to_e164(inbound.from_number) or inbound.from_number

    # Gate 0: a continuing conversation (an open sms_chat already exists for this our-DID /
    # sender pair) belongs to the 4b-2 reply engine, never auto-create — so we never fork a
    # second chat or double-reply on a later turn. When the reply flag is OFF this prevents
    # duplicate chats/replies on multi-turn conversations (the reply engine is not there to
    # absorb them); when it is ON the reply engine already handled an open chat and we are not
    # reached for it. (provider_message_id dedup only stops same-message redelivery, not a new
    # message id on a later turn.)
    if (
        await chats_repo.find_open_sms_chat(db, our_number=our_number, recipient=recipient)
        is not None
    ):
        return False

    # Gate 1: the destination DID must carry an inbound_sms_agents binding.
    pn = await phone_numbers_repo.get_by_e164(db, our_number)
    token = _pick_inbound_sms_agent(pn)
    if token is None:
        return False

    # Gate 2: never hijack a known family contact's caregiver relay (FR-008/014) — let the
    # family-task fall-through handle them.
    if await family_contacts_repo.find_contacts_by_phone(db, recipient):
        return False

    # Gate 3: the bound agent must resolve to a live published profile (else no usable binding).
    profile = await _resolve_live_profile(db, token)
    if profile is None:
        return False

    # Gate 4: a bound DID is SMS-agent territory — own the message but skip if we cannot reply.
    if not _sms_send_ready(settings) or not settings.gcp_project:
        logger.bind(message_id=inbound.message_id).warning(
            "inbound sms autocreate skipped: messaging/Vertex not configured"
        )
        _count("sms_autocreate_unconfigured")
        return True

    # Create the session + persist the inbound turn (role="sms"); the Telnyx id dedups
    # redeliveries. Persisting in the SAME txn as the session means a concurrent/duplicate
    # first delivery serializes on uq_chat_messages_provider_msg — the loser's whole txn
    # (session + message) rolls back.
    try:
        agent_version = profile.published_version
        assert agent_version is not None  # guaranteed by _resolve_live_profile
        session = await chats_repo.add_session(
            db,
            agent_profile_id=profile.id,
            agent_version=agent_version,
            dynamic_vars={},
            chat_type="sms_chat",
            from_number=our_number,
            to_number=recipient,
        )
        await db.flush()
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
        _count("sms_autocreate_dedup")
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
            "inbound sms autocreate failed"
        )
        _count("sms_autocreate_failed")
        return True

    # Commit OUTSIDE the send try: wrapping it would mislabel a commit-fail as a send-fail
    # (the reply already went out) and risk a double-send on retry (mirrors sms_reply.py).
    await db.commit()
    _count("sms_autocreate")
    return True
