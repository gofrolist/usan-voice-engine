"""Phase 4c-2: analyze_chat_with — gate, force/idempotent, parse, coercion, error swallow."""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text

from usan_api import chat_analysis
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Populate the minimum required Settings env-vars so get_settings() resolves.

    Mirrors the pattern in tests/compat/conftest.py. Values are fake; no real
    services are contacted — Vertex is always monkeypatched in these tests.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://usan:usan@localhost:5432/usan")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    yield
    get_settings.cache_clear()


def _settings(*, enabled: bool = True, project: str | None = "p"):
    return get_settings().model_copy(
        update={"chat_analysis_enabled": enabled, "gcp_project": project}
    )


async def _seed_session_with_message(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    await chats_repo.add_message(
        db, session_id=session.id, seq=1, role="user", content="I am doing great today"
    )
    await db.flush()
    return session.id


def _patch_vertex(monkeypatch, payload: str) -> None:
    async def _fake(**kwargs):
        assert kwargs["tools"] == []
        return VertexTurn(text=payload)

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _fake)


@pytest.mark.asyncio
async def test_analyze_persists_parsed_fields(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(
        monkeypatch,
        json.dumps(
            {
                "chat_summary": "A warm check-in.",
                "user_sentiment": "Positive",
                "chat_successful": True,
            }  # noqa: E501
        ),
    )
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.chat_summary == "A warm check-in."
    assert rec.user_sentiment == "Positive"
    assert rec.chat_successful is True
    await app_session.rollback()


@pytest.mark.asyncio
async def test_sentiment_off_enum_coerced_to_none(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(
        monkeypatch,
        json.dumps({"chat_summary": "x", "user_sentiment": "ecstatic", "chat_successful": "yes"}),
    )
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.user_sentiment is None  # off-enum dropped
    assert rec.chat_successful is None  # non-bool dropped
    await app_session.rollback()


@pytest.mark.asyncio
async def test_lowercase_sentiment_normalized(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(monkeypatch, json.dumps({"chat_summary": "x", "user_sentiment": "negative"}))
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.user_sentiment == "Negative"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_non_json_degrades_to_summary(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(monkeypatch, "the user was happy")  # not JSON
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.chat_summary == "the user was happy"
    assert rec.user_sentiment is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_flag_off_is_noop(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return VertexTurn(text="{}")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(
        app_session, sid, _settings(enabled=False), force=True
    )
    assert rec is None  # no prior record, no Vertex
    assert called is False
    await app_session.rollback()


@pytest.mark.asyncio
async def test_idempotent_without_force(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    await analyses_repo.upsert(
        app_session,
        sid,
        chat_summary="prior",
        user_sentiment=None,
        chat_successful=None,
        custom_analysis_data=None,
        model_version="m",
    )

    async def _boom(**kwargs):
        raise AssertionError("Vertex must not be called when a record exists and force=False")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=False)
    assert rec is not None
    assert rec.chat_summary == "prior"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_zero_messages_noop(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hi"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    app_session.add(profile)
    await app_session.flush()
    session = await chats_repo.add_session(
        app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await app_session.flush()

    async def _boom(**kwargs):
        raise AssertionError("Vertex must not be called for a zero-message chat")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(app_session, session.id, _settings(), force=True)
    assert rec is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_empty_vertex_output_persists_no_row(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(monkeypatch, "{}")  # well-formed but empty → all-None parse
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is None  # no prior record + nothing to persist
    assert await analyses_repo.get_for_session(app_session, sid) is None  # no phantom row
    await app_session.rollback()


@pytest.mark.asyncio
async def test_empty_vertex_output_keeps_prior_record(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    prior = await analyses_repo.upsert(
        app_session,
        sid,
        chat_summary="good prior",
        user_sentiment="Positive",
        chat_successful=True,
        custom_analysis_data=None,
        model_version="m",
    )
    _patch_vertex(monkeypatch, "{}")  # degenerate re-analysis must NOT wipe the good prior
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.id == prior.id
    assert rec.chat_summary == "good prior"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_vertex_error_swallowed_returns_prior(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    prior = await analyses_repo.upsert(
        app_session,
        sid,
        chat_summary="prior",
        user_sentiment=None,
        chat_successful=None,
        custom_analysis_data=None,
        model_version="m",
    )

    async def _raise(**kwargs):
        raise RuntimeError("vertex down")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _raise)
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.id == prior.id  # error swallowed → prior record returned
    await app_session.rollback()
