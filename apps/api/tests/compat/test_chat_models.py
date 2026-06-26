"""Model-level tests for ChatSession + ChatMessage (migration 0042).

Verifies:
- INSERT under an org context succeeds (GRANT to usan_app present)
- organization_id is auto-filled by the DB server_default
- UNIQUE(chat_session_id, seq) constraint is enforced
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatMessage, ChatSession
from usan_api.tenant_context import set_tenant_context


async def _seed_profile(app_session) -> AgentProfile:
    """Seed a minimal AgentProfile in the current tenant context."""
    profile = AgentProfile(
        name=f"chat-test-{uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    app_session.add(profile)
    await app_session.flush()
    return profile


@pytest.mark.asyncio
async def test_chat_session_and_message_persist_under_rls(app_session) -> None:
    """A ChatSession + ChatMessage insert under an org context round-trips; org_id auto-filled."""
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    profile = await _seed_profile(app_session)

    chat = ChatSession(
        agent_profile_id=profile.id,
        agent_version=1,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars={"name": "Pat"},
    )
    app_session.add(chat)
    await app_session.flush()

    assert chat.organization_id is not None  # DB server_default filled it

    app_session.add(ChatMessage(chat_session_id=chat.id, seq=1, role="user", content="hi"))
    await app_session.flush()

    loaded = (
        await app_session.execute(select(ChatSession).where(ChatSession.id == chat.id))
    ).scalar_one()
    assert loaded.status is ChatStatus.ONGOING
    assert loaded.chat_type == "api_chat"

    await app_session.rollback()


@pytest.mark.asyncio
async def test_chat_message_seq_is_unique_per_session(app_session) -> None:
    """A duplicate (chat_session_id, seq) violates the unique constraint."""
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    profile = await _seed_profile(app_session)

    chat = ChatSession(agent_profile_id=profile.id, agent_version=1, dynamic_vars={})
    app_session.add(chat)
    await app_session.flush()

    app_session.add(ChatMessage(chat_session_id=chat.id, seq=1, role="user", content="a"))
    await app_session.flush()

    app_session.add(ChatMessage(chat_session_id=chat.id, seq=1, role="agent", content="b"))
    with pytest.raises(IntegrityError):
        await app_session.flush()

    await app_session.rollback()
