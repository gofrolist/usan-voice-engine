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

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chats import (
    CreateChatCompletionRequest,
    CreateChatRequest,
    ListChatsRequest,
    UpdateChatRequest,
)
from usan_api.compat.serialization import (
    RESERVED_VAR_PREFIX,
    pack_dynamic_vars,
    unpack_dynamic_vars,
)
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import chats as chats_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn


def _reject_reserved(vars_: dict[str, str] | None) -> None:
    if any(str(k).startswith(RESERVED_VAR_PREFIX) for k in (vars_ or {})):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")


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


async def get_chat(db: AsyncSession, chat_id: str) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    return session


async def _load_published_config(db: AsyncSession, profile_id: uuid.UUID) -> AgentConfig:
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if version is None:
        raise CompatError(422, "agent is not available")
    return AgentConfig.model_validate(version.config)


async def create_chat_completion(
    db: AsyncSession, settings: Settings, body: CreateChatCompletionRequest
) -> list[ChatMessage]:
    # 1) lock the session row (serializes concurrent completions → safe seq)
    session = await chats_repo.lock_session(db, ids.decode_chat_id(body.chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
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
        # 5) build the system prompt from the published config + bare dynamic vars
        cfg = await _load_published_config(db, session.agent_profile_id)
        bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
        values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
        system_instruction = substitute(cfg.prompts.system_prompt, values)
        # 6) multi-turn contents (agent → genai "model"; user → "user")
        history = await chats_repo.list_messages(db, session.id)
        contents = [
            {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
            for m in history
        ]
        # 7) one text-only Vertex turn
        turn = await run_vertex_turn(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            system_instruction=system_instruction,
            tools=[],
            contents=contents,
            settings=settings,
        )
    except CompatError:
        raise
    except Exception as exc:
        # PHI/secret-safe: type name only; discard the whole uncommitted txn (no partial PHI).
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("chat completion failed")
        raise CompatError(502, "chat completion failed") from None

    # 8) persist the agent turn, commit, return ONLY the new agent message(s)
    agent_seq = await chats_repo.next_seq(db, session.id)
    agent_msg = await chats_repo.add_message(
        db, session_id=session.id, seq=agent_seq, role="agent", content=turn.text
    )
    await db.commit()
    return [agent_msg]


async def update_chat(db: AsyncSession, chat_id: str, body: UpdateChatRequest) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    _reject_reserved(body.override_dynamic_variables)
    bare_vars, metadata = unpack_dynamic_vars(session.dynamic_vars)
    if body.override_dynamic_variables is not None:
        bare_vars = body.override_dynamic_variables
    if body.metadata is not None:
        metadata = body.metadata
    session.dynamic_vars = pack_dynamic_vars(bare_vars, metadata)
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
