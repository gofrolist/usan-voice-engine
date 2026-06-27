"""Assemble the RetellAI ChatResponse object from native chat rows (Phase 4a)."""

from __future__ import annotations

from usan_api.compat import ids
from usan_api.compat.schemas.chats import CompatChat, CompatChatMessage
from usan_api.compat.serialization import to_ms, unpack_dynamic_vars
from usan_api.db.models import ChatMessage, ChatSession


def _line(message: ChatMessage) -> str:
    return f"{message.role.capitalize()}: {message.content}"


def serialize_chat(
    session: ChatSession,
    messages: list[ChatMessage],
    *,
    include_transcript: bool,
) -> CompatChat:
    """Build the RetellAI ChatResponse. include_transcript=False on the list path so
    transcript + message_with_tool_calls are omitted (V3ChatResponse forbids those keys)."""
    bare_vars, metadata = unpack_dynamic_vars(session.dynamic_vars)

    transcript: str | None = None
    message_with_tool_calls: list[CompatChatMessage] | None = None
    if include_transcript:
        transcript = "\n".join(_line(m) for m in messages)
        message_with_tool_calls = [
            CompatChatMessage(
                role=m.role,
                content=m.content,
                message_id=ids.encode_message_id(m.id),
                created_timestamp=to_ms(m.created_at) or 0,
            )
            for m in messages
        ]

    return CompatChat(
        chat_id=ids.encode_chat_id(session.id),
        agent_id=ids.encode_agent_id(session.agent_profile_id),
        chat_status=session.status.value,
        version=session.agent_version,
        chat_type=session.chat_type,
        retell_llm_dynamic_variables=bare_vars or None,
        metadata=metadata or None,
        start_timestamp=to_ms(session.started_at),
        end_timestamp=to_ms(session.ended_at),
        transcript=transcript,
        message_with_tool_calls=message_with_tool_calls,
    )
