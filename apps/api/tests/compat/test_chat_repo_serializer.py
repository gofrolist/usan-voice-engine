"""Tests for chat repository + serializer (Phase 4a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from usan_api.compat import ids
from usan_api.compat.chat_serializer import serialize_chat
from usan_api.compat.schemas.chats import ListChatsRequest
from usan_api.compat.serialization import pack_dynamic_vars
from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatMessage, ChatSession
from usan_api.repositories import chats as chats_repo
from usan_api.tenant_context import set_tenant_context


async def _seed_agent_profile(db) -> AgentProfile:
    """Seed an ACTIVE AgentProfile with published_version set under the current tenant."""
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    return profile


@pytest.mark.asyncio
async def test_next_seq_and_add_message(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    profile = await _seed_agent_profile(app_session)
    s = await chats_repo.add_session(
        app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await app_session.flush()
    assert await chats_repo.next_seq(app_session, s.id) == 1
    await chats_repo.add_message(app_session, session_id=s.id, seq=1, role="user", content="hi")
    await app_session.flush()
    assert await chats_repo.next_seq(app_session, s.id) == 2
    await app_session.rollback()


def test_serialize_chat_full_includes_transcript_and_messages():
    sid = uuid.uuid4()
    session = ChatSession(
        id=sid,
        agent_profile_id=uuid.uuid4(),
        agent_version=3,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars=pack_dynamic_vars({"name": "Pat"}, {"crm": 1}),
    )
    session.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.ended_at = None
    msgs = [
        ChatMessage(id=uuid.uuid4(), chat_session_id=sid, seq=1, role="user", content="hi"),
        ChatMessage(id=uuid.uuid4(), chat_session_id=sid, seq=2, role="agent", content="hello"),
    ]
    for m in msgs:
        m.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    out = serialize_chat(session, msgs, include_transcript=True).model_dump(exclude_none=True)
    assert out["chat_status"] == "ongoing"
    assert out["version"] == 3
    assert out["retell_llm_dynamic_variables"] == {"name": "Pat"}
    assert out["metadata"] == {"crm": 1}
    assert [m["role"] for m in out["message_with_tool_calls"]] == ["user", "agent"]
    assert out["message_with_tool_calls"][0]["message_id"].startswith("message_")
    assert "transcript" in out


def test_serialize_chat_list_item_omits_transcript_and_messages():
    sid = uuid.uuid4()
    session = ChatSession(
        id=sid,
        agent_profile_id=uuid.uuid4(),
        agent_version=1,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars={},
    )
    session.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.ended_at = None
    out = serialize_chat(session, [], include_transcript=False).model_dump(exclude_none=True)
    assert "transcript" not in out
    assert "message_with_tool_calls" not in out
    assert "retell_llm_dynamic_variables" not in out  # empty → omitted


@pytest.mark.asyncio
async def test_archived_cursor_does_not_restart_pagination(app_session) -> None:
    """A pagination_key pointing at a since-archived chat must still anchor the keyset
    (plain db.get load) so page 2 advances past it instead of restarting from the top
    and re-emitting a page-1 row. Regression for the /review #141 archived-cursor finding.
    """
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = await _seed_agent_profile(app_session)

    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    sessions = []
    for i in range(4):
        s = await chats_repo.add_session(
            app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
        )
        s.started_at = base + timedelta(minutes=i)  # sessions[3] newest, sessions[0] oldest
        sessions.append(s)
    await app_session.flush()

    # Page 1 (descending): the two newest = sessions[3], sessions[2]; cursor = sessions[2].
    page1 = await chats_repo.query_sessions(app_session, ListChatsRequest(limit=2))
    assert [x.id for x in page1] == [sessions[3].id, sessions[2].id]

    # The cursor chat is deleted (soft-delete) between pages.
    sessions[2].archived_at = datetime.now(UTC)
    await app_session.flush()

    # Page 2 with the now-archived cursor must continue past it (sessions[1], sessions[0]),
    # NOT restart and re-emit sessions[3].
    page2 = await chats_repo.query_sessions(
        app_session,
        ListChatsRequest(limit=2, pagination_key=ids.encode_chat_id(sessions[2].id)),
    )
    page2_ids = {x.id for x in page2}
    assert sessions[3].id not in page2_ids  # no duplicate of page 1
    assert sessions[2].id not in page2_ids  # archived → excluded from results
    assert page2_ids == {sessions[1].id, sessions[0].id}
    await app_session.rollback()
