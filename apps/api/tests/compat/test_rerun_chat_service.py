"""Phase 4c-2: chat_service.rerun_chat_analysis — 404, and upserts analysis under force."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from usan_api.compat import chat_service, ids
from usan_api.compat.errors import CompatError
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn


def _settings():
    return get_settings().model_copy(update={"chat_analysis_enabled": True, "gcp_project": "p"})


async def _seed_chat_with_message(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hi"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    await chats_repo.add_message(db, session_id=session.id, seq=1, role="user", content="hello")
    await db.flush()
    return session.id


@pytest.mark.asyncio
async def test_rerun_unknown_chat_404(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    with pytest.raises(CompatError) as exc:
        await chat_service.rerun_chat_analysis(
            app_session, _settings(), ids.encode_chat_id(uuid.uuid4())
        )
    assert exc.value.status_code == 404
    await app_session.rollback()


@pytest.mark.asyncio
async def test_rerun_upserts_analysis(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_chat_with_message(app_session)

    async def _fake(**kwargs):
        return VertexTurn(text=json.dumps({"chat_summary": "ok", "user_sentiment": "Neutral"}))

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _fake)
    session = await chat_service.rerun_chat_analysis(
        app_session, _settings(), ids.encode_chat_id(sid)
    )
    assert session.id == sid
    rec = await analyses_repo.get_for_session(app_session, sid)
    assert rec is not None and rec.chat_summary == "ok" and rec.user_sentiment == "Neutral"  # noqa: PT018
    await app_session.rollback()
