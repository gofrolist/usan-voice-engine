"""chat_messages gains provider_message_id + a partial-unique dedup index (Phase 4b-2)."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, ChatMessage, ChatSession
from usan_api.tenant_context import set_tenant_context


async def _seed_session(db) -> ChatSession:
    profile = AgentProfile(
        name=f"SMS Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    s = ChatSession(
        agent_profile_id=profile.id, agent_version=1, chat_type="sms_chat", dynamic_vars={}
    )
    db.add(s)
    await db.flush()
    return s


def test_migration_0044_revision_header() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0044_chat_provider_message_id.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0044", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0044"
    assert mod.down_revision == "0043"


@pytest.mark.asyncio
async def test_provider_message_id_persists_and_dedups(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    s = await _seed_session(app_session)

    app_session.add(
        ChatMessage(
            chat_session_id=s.id, seq=1, role="sms", content="hi", provider_message_id="tx-1"
        )
    )
    await app_session.flush()
    # a duplicate provider id in the same org violates the partial unique
    app_session.add(
        ChatMessage(
            chat_session_id=s.id, seq=2, role="sms", content="dup", provider_message_id="tx-1"
        )
    )
    with pytest.raises(IntegrityError):
        await app_session.flush()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_null_provider_message_id_is_not_deduped(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    s = await _seed_session(app_session)
    # two NULL provider ids coexist (api_chat rows) — the partial index excludes NULLs
    app_session.add(ChatMessage(chat_session_id=s.id, seq=1, role="user", content="a"))
    app_session.add(ChatMessage(chat_session_id=s.id, seq=2, role="agent", content="b"))
    await app_session.flush()
    await app_session.rollback()
