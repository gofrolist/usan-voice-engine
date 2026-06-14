import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.repositories import agent_profiles as repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    # These repo tests commit profile rows (defaults persist across tests) and never
    # go through the `client` fixture that truncates. Reset profile state per test so
    # "nothing resolvable" assertions see a clean DB regardless of run order.
    # NOTE: this hand-rolled TRUNCATE set must stay in sync with conftest's truncation
    # set — if a future migration adds a table referencing agent_profiles, add it here
    # (and to conftest) or cross-file leakage / run-order flakiness can return.
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE agent_profile_versions, agent_profiles RESTART IDENTITY CASCADE")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    # Teardown truncate too: committed default profiles otherwise leak into other test
    # files (e.g. test_runtime.py's "nothing configured" assertion), making them pass
    # only by run-order luck. Clean up on both ends so neither file leaks into the other.
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE agent_profile_versions, agent_profiles RESTART IDENTITY CASCADE")
        )
    await engine.dispose()


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def _published(db, *, voice_id: str) -> uuid.UUID:
    """Create a profile, set a distinctive voice id, publish it. Returns the id."""
    profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
    cfg = dict(profile.draft_config)
    cfg["voice"] = {**cfg["voice"], "cartesia_voice_id": voice_id}
    await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
    await repo.publish(db, profile.id, note="v1", actor_email="op")
    return profile.id


async def test_get_default_profile_returns_active_default(session_factory):
    async with session_factory() as db:
        pid = await _published(db, voice_id="vd")
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        found = await repo.get_default_profile(db, "outbound")
        assert found is not None
        assert found.id == pid
        assert await repo.get_default_profile(db, "inbound") is None


async def test_resolve_uses_direction_default_when_no_override_or_elder(session_factory):
    async with session_factory() as db:
        pid = await _published(db, voice_id="default-voice")
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        resolved = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert resolved is not None
        assert resolved.source == "resolved"
        assert resolved.profile_id == pid
        assert resolved.version == 1
        assert resolved.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_prefers_override_then_elder_then_default(session_factory):
    async with session_factory() as db:
        override = await _published(db, voice_id="override-voice")
        elder = await _published(db, voice_id="elder-voice")
        default = await _published(db, voice_id="default-voice")
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=override, elder_profile_id=elder, direction="outbound"
        )
        assert r is not None
        assert r.config.voice.cartesia_voice_id == "override-voice"
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=elder, direction="outbound"
        )
        assert r is not None
        assert r.config.voice.cartesia_voice_id == "elder-voice"


async def test_resolve_falls_through_unpublished_candidate(session_factory):
    # An override pointing at a never-published profile must fall through to elder/default.
    async with session_factory() as db:
        unpublished = await repo.create_profile(
            db, name=_name(), description=None, actor_email="op"
        )
        default = await _published(db, voice_id="default-voice")
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
        uid = unpublished.id
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=uid, elder_profile_id=None, direction="outbound"
        )
        assert r is not None
        assert r.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_skips_archived_candidate(session_factory):
    async with session_factory() as db:
        archived = await _published(db, voice_id="archived-voice")
        default = await _published(db, voice_id="default-voice")
        await repo.archive_profile(db, archived)
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
        aid = archived
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=aid, elder_profile_id=None, direction="outbound"
        )
        assert r is not None
        assert r.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_returns_none_when_nothing_resolvable(session_factory):
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert r is None


async def test_get_published_config_none_when_unpublished(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        assert await repo.get_published_config(db, profile) is None


async def test_resolve_degrades_when_default_config_invalid(session_factory):
    # The _resolved_from_profile `except ValidationError` safety branch: a published
    # default whose stored JSON no longer validates must fall through (and, with nothing
    # else resolvable, the walk returns None) rather than raising.
    async with session_factory() as db:
        pid = await _published(db, voice_id="default-voice")
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        # Corrupt the stored version config so AgentConfig.model_validate raises.
        await db.execute(text("UPDATE agent_profile_versions SET config = '{}'::jsonb"))
        await db.commit()
    async with session_factory() as db:
        resolved = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert resolved is None  # invalid default falls through; nothing else resolves
