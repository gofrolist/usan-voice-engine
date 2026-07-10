"""Service-level tests for compat.chat_service (Phase 4a + 5b)."""

from __future__ import annotations

import copy
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
from usan_api.repositories import chats as chats_repo
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


async def _seed_session_with_kb(
    db,
    org_id: uuid.UUID,
    *,
    kb_ids: list[str],
    user_text: str,
):
    """Seed a published profile with knowledge_base_ids set, a ChatSession, and one user message.
    Returns the ChatSession (not yet committed — tests own the transaction lifecycle)."""
    cfg = copy.deepcopy(_VALID_CONFIG)
    cfg["llm"]["knowledge_base_ids"] = kb_ids

    profile = AgentProfile(
        name=f"KB Test Agent {uuid.uuid4().hex[:8]}",
        draft_config=cfg,
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()

    version = AgentProfileVersion(
        profile_id=profile.id,
        version=1,
        config=cfg,
    )
    db.add(version)
    await db.flush()

    from usan_api.compat.serialization import pack_dynamic_vars

    session = await chats_repo.add_session(
        db,
        agent_profile_id=profile.id,
        agent_version=profile.published_version,
        dynamic_vars=pack_dynamic_vars(None, None),
    )
    await db.flush()

    seq = await chats_repo.next_seq(db, session.id)
    await chats_repo.add_message(db, session_id=session.id, seq=seq, role="user", content=user_text)
    await db.flush()
    return session


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


@pytest.mark.asyncio
async def test_generate_agent_reply_injects_kb_context(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.compat.kb_retrieval import RetrievedContext
    from usan_api.vertex_test import VertexTurn

    captured: dict = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def fake_retrieve(db, settings, *, kb_ids, query, enabled):
        captured["query"] = query
        return RetrievedContext(text="DOC_CONTEXT", hit_count=1)

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", fake_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="my question"
    )

    reply = await chat_service.generate_agent_reply(app_session, settings, session)
    assert reply == "answer"
    assert "DOC_CONTEXT" in captured["system_instruction"]
    assert "Knowledge base context:" in captured["system_instruction"]
    assert captured["query"] == "my question"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_generate_agent_reply_no_kb_context_when_no_match(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.compat.kb_retrieval import RetrievedContext
    from usan_api.vertex_test import VertexTurn

    captured: dict = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def fake_retrieve(db, settings, *, kb_ids, query, enabled):
        return RetrievedContext(text="", hit_count=0)

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", fake_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="q"
    )
    await chat_service.generate_agent_reply(app_session, settings, session)
    assert "Knowledge base context:" not in captured["system_instruction"]
    await app_session.rollback()


@pytest.mark.asyncio
async def test_generate_agent_reply_degrades_on_retrieval_error(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.vertex_test import VertexTurn

    captured: dict = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def boom_retrieve(db, settings, *, kb_ids, query, enabled):
        raise RuntimeError("vertex 429")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", boom_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="q"
    )
    reply = await chat_service.generate_agent_reply(app_session, settings, session)
    assert reply == "answer"  # retrieval failure never breaks the reply
    assert "Knowledge base context:" not in captured["system_instruction"]
    await app_session.rollback()


@pytest.mark.asyncio
async def test_create_chat_completion_survives_db_abort_in_retrieval(
    app_session, monkeypatch
) -> None:
    """Regression: a DB-aborting statement inside retrieve_context must NOT poison the
    caller's transaction. With the savepoint fix, create_chat_completion succeeds and the
    flushed user turn is still present; without the fix it would raise PendingRollbackError
    on the post-retrieval agent-turn persist and 500 the request."""
    from usan_api.compat.kb_retrieval import RetrievedContext
    from usan_api.vertex_test import VertexTurn

    async def poison_retrieve(db, settings, *, kb_ids, query, enabled):
        # Runs a genuinely aborting statement to enter the "failed transaction" state,
        # exactly as a Cloud SQL statement-timeout / pgvector error would.
        await db.execute(text("SELECT 1/0"))
        return RetrievedContext("", 0)

    async def fake_turn(**kwargs):
        return VertexTurn(text="agent reply")

    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", poison_retrieve)
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)

    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    # Seed a profile whose published config binds a kb id, plus a chat session
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc123456789012345678901"], user_text="hi"
    )
    # Commit the seed (creates the session row); re-apply tenant context
    await app_session.commit()
    await set_tenant_context(app_session, org_id)
    chat_id = encode_chat_id(session.id)

    # Drive the FULL create_chat_completion (not generate_agent_reply directly)
    msgs = await chat_service.create_chat_completion(
        app_session,
        settings,
        CreateChatCompletionRequest(chat_id=chat_id, content="hello"),
    )

    # Agent reply returned normally — no exception, no 500
    assert len(msgs) == 1
    assert msgs[0].role == "agent"
    assert msgs[0].content == "agent reply"

    # create_chat_completion commits internally (is_local set_config is cleared); re-apply
    # tenant context so RLS allows the subsequent list_messages SELECT.
    await set_tenant_context(app_session, org_id)

    # The user turn seeded by create_chat_completion is still present (savepoint preserved
    # the outer transaction — it was NOT rolled back along with the aborting retrieval)
    from usan_api.repositories import chats as chats_repo

    history = await chats_repo.list_messages(app_session, session.id)
    roles = [m.role for m in history]
    # history: original "user" seed (from _seed_session_with_kb) + new "user" + "agent"
    assert "user" in roles
    assert "agent" in roles
    await app_session.rollback()
