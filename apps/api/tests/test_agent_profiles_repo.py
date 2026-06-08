import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def test_create_then_publish_increments_version(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        await db.commit()
        pid = profile.id
        assert profile.published_version is None

    async with session_factory() as db:
        v1 = await repo.publish(db, pid, note="first", actor_email="op")
        await db.commit()
        assert v1 is not None
        assert v1.version == 1

    async with session_factory() as db:
        v2 = await repo.publish(db, pid, note="second", actor_email="op")
        await db.commit()
        assert v2 is not None
        assert v2.version == 2
        refreshed = await repo.get_profile(db, pid)
        assert refreshed is not None
        assert refreshed.published_version == 2


async def test_rollback_republishes_old_config(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        pid = profile.id
        await repo.publish(db, pid, note="v1", actor_email="op")  # version 1
        changed = DEFAULT_AGENT_CONFIG.model_copy(
            update={"llm": DEFAULT_AGENT_CONFIG.llm.model_copy(update={"model": "x-2"})}
        )
        await repo.update_draft(
            db, pid, config=changed.model_dump(), description=None, actor_email="op"
        )
        await repo.publish(db, pid, note="v2", actor_email="op")  # version 2
        await db.commit()

    async with session_factory() as db:
        v3 = await repo.rollback(db, pid, target_version=1, actor_email="op")
        await db.commit()
        assert v3 is not None
        assert v3.version == 3  # rollback creates a NEW version
        assert v3.config["llm"]["model"] == "gemini-3.1-flash-lite"


async def test_set_default_is_exclusive_per_direction(session_factory):
    async with session_factory() as db:
        a = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        b = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        await db.commit()
        aid, bid = a.id, b.id

    async with session_factory() as db:
        await repo.set_default(db, aid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        await repo.set_default(db, bid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        a2 = await repo.get_profile(db, aid)
        b2 = await repo.get_profile(db, bid)
        assert a2 is not None
        assert b2 is not None
        assert a2.is_default_outbound is False
        assert b2.is_default_outbound is True


async def test_archive_blocked_when_default(session_factory):
    async with session_factory() as db:
        p = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        pid = p.id
        await repo.set_default(db, pid, direction="inbound")
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(ProfileInUseError):
            await repo.archive_profile(db, pid)
