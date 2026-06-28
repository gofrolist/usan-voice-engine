"""Compat chat-session service layer (Phase 4a).

create_chat_completion runs ONE Vertex text turn (text-only, tools=[]) reusing the
in-apps/api Vertex path — no LiveKit, no services/agent import. PHI/secret-safe: caught
exceptions re-raise as CompatError from None; the global handler logs type(exc).__name__.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import chat_analysis, telnyx_messaging
from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chats import (
    CreateChatCompletionRequest,
    CreateChatRequest,
    CreateSmsChatRequest,
    ListChatsRequest,
    UpdateChatRequest,
)
from usan_api.compat.serialization import (
    RESERVED_VAR_PREFIX,
    carry_unhonored,
    pack_dynamic_vars,
    unpack_dynamic_vars,
)
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession
from usan_api.phone import to_e164
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import chats as chats_repo
from usan_api.repositories import phone_numbers as phone_numbers_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn


def _reject_reserved(vars_: dict[str, str] | None) -> None:
    if any(str(k).startswith(RESERVED_VAR_PREFIX) for k in (vars_ or {})):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")


def _sms_send_ready(settings: Settings) -> bool:
    """True iff outbound SMS can be sent: the feature flag is on and the three Telnyx
    messaging secrets are present. The 503 gate in create_sms_chat checks this before any
    write (send_sms itself would otherwise raise after a row was already written)."""
    return bool(
        settings.telnyx_messaging_enabled
        and settings.telnyx_messaging_api_key
        and settings.telnyx_messaging_profile_id
        and settings.telnyx_from_number
    )


async def create_chat(db: AsyncSession, body: CreateChatRequest) -> ChatSession:
    profile_id = ids.decode_agent_id(body.agent_id)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent_id must reference a published agent")
    _reject_reserved(body.retell_llm_dynamic_variables)

    profile = await agent_profiles_repo.get_profile(db, profile_id)
    assert profile is not None  # is_live_profile guaranteed
    assert profile.published_version is not None  # is_live_profile guaranteed
    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    session = await chats_repo.add_session(
        db,
        agent_profile_id=profile_id,
        agent_version=profile.published_version,
        dynamic_vars=packed,
    )
    await db.commit()
    return session


async def _resolve_sms_agent(db: AsyncSession, body: CreateSmsChatRequest) -> uuid.UUID:
    """override_agent_id wins (one-time override). Otherwise honor the from_number's
    outbound_sms_agents[0] binding WITHIN the caller's org (RLS-safe, same-org — not the
    deferred cross-org inbound case). 422 if no live agent resolves."""
    if body.override_agent_id:
        profile_id = ids.decode_agent_id(body.override_agent_id)
    else:
        pn = await phone_numbers_repo.get_by_e164(db, body.from_number)
        agents = (pn.outbound_sms_agents if pn is not None else None) or []
        token = (agents[0] or {}).get("agent_id") if agents else None
        if not isinstance(token, str) or not token:
            raise CompatError(422, "no agent bound to from_number")
        profile_id = ids.decode_agent_id(token)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent must reference a published agent")
    return profile_id


async def create_sms_chat(
    db: AsyncSession, settings: Settings, body: CreateSmsChatRequest
) -> ChatSession:
    # 1) config gate — BEFORE any write
    if not _sms_send_ready(settings):
        raise CompatError(503, "sms messaging is not configured")
    # 2) from_number must be our single provisioned sender
    if body.from_number != settings.telnyx_from_number:
        raise CompatError(422, "from_number is not a provisioned sender")
    # 3) resolve the agent (override -> same-org binding -> 422), then guard reserved vars
    profile_id = await _resolve_sms_agent(db, body)
    _reject_reserved(body.retell_llm_dynamic_variables)
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    assert profile is not None  # is_live_profile guaranteed
    assert profile.published_version is not None  # is_live_profile guaranteed
    # 4) initial message = the agent's configured greeting with dynamic vars substituted
    cfg = await _load_published_config(db, profile_id)
    # The greeting is recipient-facing outbound text, so render clock vars against the
    # compat default timezone (matching the compat voice path call_create.py), not "" —
    # else {{current_time}}/{{current_date}} would render blank in the SMS.
    values = build_vars(
        {},
        body.retell_llm_dynamic_variables or {},
        timezone=settings.compat_default_timezone,
        now=datetime.now(UTC),
    )
    greeting = substitute(cfg.prompts.greeting, values)
    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    # 5) persist session + greeting, send via Telnyx; ANY failure rolls back the whole txn
    # Normalize BOTH numbers to E.164 so the stored row matches what the inbound matcher
    # (find_open_sms_chat) compares against — it normalizes the inbound from/to via to_e164.
    # from_number must equal the provisioned sender (validated above); normalizing it too
    # keeps matching correct even if TELNYX_FROM_NUMBER is configured non-strict-E.164.
    from_number = to_e164(body.from_number) or body.from_number
    to_number = to_e164(body.to_number) or body.to_number
    try:
        session = await chats_repo.add_session(
            db,
            agent_profile_id=profile_id,
            agent_version=profile.published_version,
            dynamic_vars=packed,
            chat_type="sms_chat",
            from_number=from_number,
            to_number=to_number,
        )
        await db.flush()
        seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=seq, role="agent", content=greeting
        )
        await db.flush()
        await telnyx_messaging.send_sms(settings, to_number=to_number, body=greeting)
    except CompatError:
        raise
    except Exception as exc:
        # PHI/secret-safe: type name only; discard the whole uncommitted txn (no orphan row).
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("create sms chat failed")
        raise CompatError(502, "sms send failed") from None
    # 6) commit; the router serializes (incl. the sent greeting) via _serialize_full
    await db.commit()
    return session


async def get_chat(db: AsyncSession, chat_id: str) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    return session


async def rerun_chat_analysis(db: AsyncSession, settings: Settings, chat_id: str) -> ChatSession:
    """Recompute the chat's post-chat analysis inline and return the session (the router
    serializes the fresh analysis). 404 if the chat is missing or archived (RLS scopes the
    lookup to the caller's org, so a cross-org chat_id is a clean 404)."""
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    await chat_analysis.analyze_chat_with(db, session.id, settings, force=True)
    await db.flush()
    return session


async def _load_published_config(db: AsyncSession, profile_id: uuid.UUID) -> AgentConfig:
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if version is None:
        raise CompatError(422, "agent is not available")
    return AgentConfig.model_validate(version.config)


async def generate_agent_reply(db: AsyncSession, settings: Settings, session: ChatSession) -> str:
    """Load the published config, build the system prompt + multi-turn contents from the
    FULL message history, run ONE text-only Vertex turn, return the reply text. The caller
    must have already persisted+flushed the latest user/sms turn so it appears in history.
    Raises on Vertex failure (the caller owns rollback). The role map sends "agent" turns as
    genai "model" and every other role ("user"/"sms") as "user"."""
    cfg = await _load_published_config(db, session.agent_profile_id)
    bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
    values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)
    history = await chats_repo.list_messages(db, session.id)
    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in history
    ]
    turn = await run_vertex_turn(
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        system_instruction=system_instruction,
        tools=[],
        contents=contents,
        settings=settings,
    )
    return turn.text


async def create_chat_completion(
    db: AsyncSession, settings: Settings, body: CreateChatCompletionRequest
) -> list[ChatMessage]:
    # 1) lock the session row (serializes concurrent completions → safe seq)
    session = await chats_repo.lock_session(db, ids.decode_chat_id(body.chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    # reject api_chat-style synchronous completion on an sms_chat (SMS replies are
    # webhook-driven; never injected through this endpoint). 4b-2 will drive sms replies.
    if session.chat_type == "sms_chat":
        raise CompatError(422, "cannot complete an sms chat")
    # 2) gate on status
    if session.status is not ChatStatus.ONGOING:
        raise CompatError(422, "chat is not ongoing")
    # 3) Vertex config gate — BEFORE any write
    if not settings.gcp_project:
        raise CompatError(503, "chat completion unavailable")

    try:
        # 4) persist the user turn
        user_seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=user_seq, role="user", content=body.content
        )
        await db.flush()
        # 5) one text-only Vertex turn over the full history (shared with the 4b-2 sms path)
        turn_text = await generate_agent_reply(db, settings, session)
    except CompatError:
        raise
    except Exception as exc:
        # PHI/secret-safe: type name only; discard the whole uncommitted txn (no partial PHI).
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("chat completion failed")
        raise CompatError(502, "chat completion failed") from None

    # 6) persist the agent turn, commit, return ONLY the new agent message(s)
    agent_seq = await chats_repo.next_seq(db, session.id)
    agent_msg = await chats_repo.add_message(
        db, session_id=session.id, seq=agent_seq, role="agent", content=turn_text
    )
    await db.commit()
    return [agent_msg]


async def update_chat(db: AsyncSession, chat_id: str, body: UpdateChatRequest) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    _reject_reserved(body.override_dynamic_variables)
    old_dv = session.dynamic_vars
    bare_vars, metadata = unpack_dynamic_vars(old_dv)
    if body.override_dynamic_variables is not None:
        bare_vars = body.override_dynamic_variables
    if body.metadata is not None:
        metadata = body.metadata
    # carry_unhonored preserves any reserved __meta_unhonored__ audit blob across the
    # unpack->repack (mirrors update_call); a no-op in 4a where nothing stashes it.
    session.dynamic_vars = carry_unhonored(old_dv, pack_dynamic_vars(bare_vars, metadata))
    if body.custom_attributes is not None:
        session.custom_attributes = body.custom_attributes
    # data_storage_setting is accepted-and-ignored (4a).
    await db.commit()
    await db.refresh(session)
    return session


async def end_chat(db: AsyncSession, chat_id: str) -> None:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    session.status = ChatStatus.ENDED
    session.ended_at = datetime.now(UTC)
    await db.commit()


async def delete_chat(db: AsyncSession, chat_id: str) -> None:
    try:
        session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    except CompatError:
        raise CompatError(404, "chat not found") from None
    if session is None:
        raise CompatError(404, "chat not found")
    session.archived_at = func.now()
    await db.commit()


async def list_chats(
    db: AsyncSession, body: ListChatsRequest
) -> tuple[list[ChatSession], str | None, bool, int | None]:
    sessions = await chats_repo.query_sessions(db, body)
    pagination_key = ids.encode_chat_id(sessions[-1].id) if sessions else None
    has_more = len(sessions) == body.limit
    total = await chats_repo.count_sessions(db, body) if body.include_total else None
    return sessions, pagination_key, has_more, total
