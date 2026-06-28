"""Phase 4c-1: repo channel filters/guards keep chat rows out of the voice call plane."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from usan_api.db.models import AgentProfile
from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.tenant_context import set_tenant_context


async def _org(app_session):
    from sqlalchemy import text

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    return org_id


async def _make_published(session, *, channel: str, name: str) -> object:
    profile = await repo.create_profile(
        session, name=name, description=None, actor_email="t@example.com"
    )
    profile.channel = channel
    await session.flush()
    await repo.publish(session, profile.id, note="seed", actor_email="t@example.com")
    await session.flush()
    return profile


@pytest.mark.asyncio
async def test_list_profiles_channel_filter(app_session):
    await _org(app_session)
    voice = await _make_published(app_session, channel="voice", name="v1")
    chat = await _make_published(app_session, channel="chat", name="c1")
    voice_only = {p.id for p in await repo.list_profiles(app_session, channel="voice")}
    assert voice.id in voice_only
    assert chat.id not in voice_only
    all_rows = {p.id for p in await repo.list_profiles(app_session)}
    assert voice.id in all_rows  # no channel = both
    assert chat.id in all_rows


@pytest.mark.asyncio
async def test_is_live_profile_channel(app_session):
    await _org(app_session)
    chat = await _make_published(app_session, channel="chat", name="c2")
    assert await repo.is_live_profile(app_session, chat.id) is True  # agnostic
    assert await repo.is_live_profile(app_session, chat.id, channel="voice") is False
    assert await repo.is_live_profile(app_session, chat.id, channel="chat") is True


@pytest.mark.asyncio
async def test_set_default_rejects_chat(app_session):
    await _org(app_session)
    chat = await _make_published(app_session, channel="chat", name="c3")
    with pytest.raises(ProfileInUseError):
        await repo.set_default(app_session, chat.id, direction="outbound")


@pytest.mark.asyncio
async def test_get_default_profile_ignores_chat(app_session):
    await _org(app_session)
    chat = await _make_published(app_session, channel="chat", name="c4")
    # Clear any pre-existing default-outbound holder first (a neighboring test may have committed
    # one under -n0; app_session rolls this back after the test). Without it, forcing the chat
    # flag could collide with the partial-unique index OR a leftover voice default could be
    # returned — both isolation artifacts unrelated to the channel filter under test.
    await app_session.execute(
        update(AgentProfile)
        .where(AgentProfile.is_default_outbound.is_(True))
        .values(is_default_outbound=False)
    )
    await app_session.flush()
    # Force the chat row to hold the default flag (bypassing set_default's guard) to prove the
    # READ filter also excludes it.
    chat.is_default_outbound = True
    await app_session.flush()
    assert await repo.get_default_profile(app_session, "outbound") is None


@pytest.mark.asyncio
async def test_resolved_from_profile_skips_chat(app_session):
    await _org(app_session)
    chat = await _make_published(app_session, channel="chat", name="c5")
    profile = await repo.get_profile(app_session, chat.id)
    assert await repo._resolved_from_profile(app_session, profile) is None
