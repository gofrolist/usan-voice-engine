"""Phase 4c-1: voice callers reject a chat profile; admin /profiles list excludes chat."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from usan_api.repositories import agent_profiles as repo
from usan_api.services.outbound_calls import require_live_override
from usan_api.tenant_context import set_tenant_context


async def _org(app_session) -> uuid.UUID:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    return org_id


async def _published_chat(session, name: str) -> object:
    profile = await repo.create_profile(
        session, name=name, description=None, actor_email="t@example.com"
    )
    profile.channel = "chat"
    await session.flush()
    await repo.publish(session, profile.id, note="seed", actor_email="t@example.com")
    await session.flush()
    return profile


@pytest.mark.asyncio
async def test_require_live_override_rejects_chat(app_session):
    await _org(app_session)
    chat = await _published_chat(app_session, "chat-caller-1")
    with pytest.raises(HTTPException) as exc:
        await require_live_override(app_session, chat.id)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_admin_profiles_list_excludes_chat(app_session):
    """Repo-level proxy for the admin-ui list (the handler calls list_profiles(channel='voice'))."""
    await _org(app_session)
    chat = await _published_chat(app_session, "chat-caller-2")
    voice_only = {p.id for p in await repo.list_profiles(app_session, channel="voice")}
    assert chat.id not in voice_only
