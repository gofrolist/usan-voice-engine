"""Chat session/message persistence (Phase 4a). RLS-scoped; org_id auto-filled by DB default."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chats import ListChatsRequest
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession


async def add_session(
    db: AsyncSession,
    *,
    agent_profile_id: uuid.UUID,
    agent_version: int,
    dynamic_vars: dict[str, Any],
) -> ChatSession:
    session = ChatSession(
        agent_profile_id=agent_profile_id,
        agent_version=agent_version,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars=dynamic_vars,
    )
    db.add(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    session = await db.get(ChatSession, session_id)
    if session is None or session.archived_at is not None:
        return None
    return session


async def lock_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    """Load the session FOR UPDATE so concurrent completions serialize (seq safety)."""
    session = await db.get(ChatSession, session_id, with_for_update=True)
    if session is None or session.archived_at is not None:
        return None
    return session


async def next_seq(db: AsyncSession, session_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(ChatMessage.seq), 0) + 1).where(
        ChatMessage.chat_session_id == session_id
    )
    return int((await db.execute(stmt)).scalar_one())


async def add_message(
    db: AsyncSession, *, session_id: uuid.UUID, seq: int, role: str, content: str
) -> ChatMessage:
    message = ChatMessage(chat_session_id=session_id, seq=seq, role=role, content=content)
    db.add(message)
    return message


async def list_messages(db: AsyncSession, session_id: uuid.UUID) -> list[ChatMessage]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.chat_session_id == session_id)
        .order_by(ChatMessage.seq.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _base_query(body: ListChatsRequest) -> Select[tuple[ChatSession]]:
    stmt = select(ChatSession).where(ChatSession.archived_at.is_(None))
    fc = body.filter_criteria or {}
    agent = fc.get("agent_id")
    if isinstance(agent, str) and agent:
        try:
            stmt = stmt.where(ChatSession.agent_profile_id == ids.decode_agent_id(agent))
        except CompatError:
            stmt = stmt.where(ChatSession.id == uuid.UUID(int=0))  # matches nothing
    status = fc.get("chat_status")
    if isinstance(status, str) and status in {s.value for s in ChatStatus}:
        stmt = stmt.where(ChatSession.status == ChatStatus(status))
    return stmt


async def query_sessions(db: AsyncSession, body: ListChatsRequest) -> list[ChatSession]:
    stmt = _base_query(body)
    descending = body.sort_order != "ascending"
    if body.pagination_key:
        try:
            cursor = await get_session(db, ids.decode_chat_id(body.pagination_key))
        except CompatError:
            cursor = None
        if cursor is not None:
            if descending:
                stmt = stmt.where(
                    or_(
                        ChatSession.started_at < cursor.started_at,
                        and_(
                            ChatSession.started_at == cursor.started_at,
                            ChatSession.id < cursor.id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        ChatSession.started_at > cursor.started_at,
                        and_(
                            ChatSession.started_at == cursor.started_at,
                            ChatSession.id > cursor.id,
                        ),
                    )
                )
    if descending:
        stmt = stmt.order_by(ChatSession.started_at.desc(), ChatSession.id.desc())
    else:
        stmt = stmt.order_by(ChatSession.started_at.asc(), ChatSession.id.asc())
    if body.skip:
        stmt = stmt.offset(body.skip)
    stmt = stmt.limit(body.limit)
    return list((await db.execute(stmt)).scalars().all())


async def count_sessions(db: AsyncSession, body: ListChatsRequest) -> int:
    inner = _base_query(body).subquery()
    return int((await db.execute(select(func.count()).select_from(inner))).scalar_one())
