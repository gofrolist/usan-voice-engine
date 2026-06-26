"""Service-level tests for compat.chat_service (Phase 4a)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from usan_api.compat import chat_service
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_agent_id, encode_chat_id
from usan_api.compat.schemas.chats import CreateChatCompletionRequest, CreateChatRequest
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

# A valid published config dict — use the default so AgentConfig.model_validate passes.
_VALID_CONFIG = DEFAULT_AGENT_CONFIG.model_dump()


async def _seed_published_profile(db) -> AgentProfile:
    """Seed an ACTIVE AgentProfile with a published version so is_live_profile passes."""
    profile = AgentProfile(
        name=f"Chat Test Agent {uuid.uuid4().hex[:8]}",
        draft_config=_VALID_CONFIG,
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()

    version = AgentProfileVersion(
        profile_id=profile.id,
        version=1,
        config=_VALID_CONFIG,
    )
    db.add(version)
    await db.flush()
    return profile


async def _seed_chat(db, org_id, agent_token: str) -> str:
    """Create a chat session and re-establish tenant context (commit clears is_local set_config)."""
    body = CreateChatRequest(agent_id=agent_token)
    session = await chat_service.create_chat(db, body)
    chat_id = encode_chat_id(session.id)
    # create_chat commits, which clears the transaction-local tenant set_config; restore it.
    await set_tenant_context(db, org_id)
    return chat_id


@pytest.mark.asyncio
async def test_create_chat_rejects_unpublished_agent(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    with pytest.raises(CompatError) as exc:
        await chat_service.create_chat(app_session, CreateChatRequest(agent_id="agent_" + "0" * 32))
    assert exc.value.status_code == 422
    await app_session.rollback()


@pytest.mark.asyncio
async def test_completion_503_when_gcp_unset_persists_nothing(app_session, monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", spy)
    settings_no_gcp = get_settings().model_copy(update={"gcp_project": None})

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    profile = await _seed_published_profile(app_session)
    agent_token = encode_agent_id(profile.id)
    chat_id = await _seed_chat(app_session, org_id, agent_token)

    body = CreateChatCompletionRequest(chat_id=chat_id, content="hi")
    with pytest.raises(CompatError) as exc:
        await chat_service.create_chat_completion(app_session, settings_no_gcp, body)
    assert exc.value.status_code == 503
    spy.assert_not_awaited()

    # no messages persisted (the 503 fires before any write)
    n = (await app_session.execute(text("SELECT count(*) FROM chat_messages"))).scalar_one()
    assert n == 0
    await app_session.rollback()


@pytest.mark.asyncio
async def test_completion_returns_only_new_agent_message(app_session, monkeypatch) -> None:
    from usan_api.vertex_test import VertexTurn

    async def fake_turn(**kwargs):
        assert kwargs["tools"] == []
        # the prior user turn must be present as a genai "user" content
        assert kwargs["contents"][-1]["role"] == "user"
        return VertexTurn(text="hello there")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    settings_gcp = get_settings().model_copy(update={"gcp_project": "test-project"})

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    profile = await _seed_published_profile(app_session)
    agent_token = encode_agent_id(profile.id)
    chat_id = await _seed_chat(app_session, org_id, agent_token)

    new = await chat_service.create_chat_completion(
        app_session, settings_gcp, CreateChatCompletionRequest(chat_id=chat_id, content="hi")
    )
    assert [m.role for m in new] == ["agent"]
    assert new[0].content == "hello there"
    await app_session.rollback()
