"""Phase 4c-1: agent_profiles.channel defaults to 'voice' (migration 0045 backfill)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from usan_api.repositories import agent_profiles as repo
from usan_api.tenant_context import set_tenant_context


async def _org(app_session):
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    return org_id


@pytest.mark.asyncio
async def test_new_profile_defaults_channel_voice(app_session):
    await _org(app_session)
    profile = await repo.create_profile(
        app_session, name="chan-default", description=None, actor_email="t@example.com"
    )
    assert profile.channel == "voice"
