"""ChatSession gains nullable from_number/to_number for sms_chat rows (Phase 4b-1)."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.tenant_context import set_tenant_context


async def _seed_agent_profile(db) -> AgentProfile:
    profile = AgentProfile(
        name=f"SMS Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    return profile


@pytest.mark.asyncio
async def test_chat_session_persists_from_and_to_number(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = await _seed_agent_profile(app_session)

    s = ChatSession(
        agent_profile_id=profile.id,
        agent_version=1,
        chat_type="sms_chat",
        dynamic_vars={},
        from_number="+15550001111",
        to_number="+15550002222",
    )
    app_session.add(s)
    await app_session.flush()

    loaded = await app_session.get(ChatSession, s.id)
    assert loaded is not None
    assert loaded.chat_type == "sms_chat"
    assert loaded.from_number == "+15550001111"
    assert loaded.to_number == "+15550002222"
    await app_session.rollback()


def test_migration_0043_revision_header() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0043_chat_sms_numbers.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0043", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0043"
    assert mod.down_revision == "0042"


@pytest.mark.asyncio
async def test_add_session_sets_chat_type_and_numbers(app_session) -> None:
    from usan_api.repositories import chats as chats_repo

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = await _seed_agent_profile(app_session)

    s = await chats_repo.add_session(
        app_session,
        agent_profile_id=profile.id,
        agent_version=1,
        dynamic_vars={},
        chat_type="sms_chat",
        from_number="+15550001111",
        to_number="+15550002222",
    )
    await app_session.flush()
    assert s.chat_type == "sms_chat"
    assert s.from_number == "+15550001111"
    assert s.to_number == "+15550002222"

    d = await chats_repo.add_session(
        app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await app_session.flush()
    assert d.chat_type == "api_chat"
    assert d.from_number is None
    assert d.to_number is None
    await app_session.rollback()
