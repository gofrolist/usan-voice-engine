"""follow_up_flags repository: create + filtered list (mirrors wellness repo)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import follow_up_flags as repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call_and_elder(factory) -> tuple[uuid.UUID, uuid.UUID]:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Flag Elder", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
        )
        await db.commit()
        return call.id, elder.id


async def test_create_and_list_follow_up_flag(session_factory):
    cid, eid = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        row = await repo.create_follow_up_flag(
            db,
            call_id=cid,
            elder_id=eid,
            severity="urgent",
            category="medical",
            reason="chest pain reported",
        )
        await db.commit()
        assert isinstance(row.id, int)
        assert row.status == "open"
        assert row.severity == "urgent"

        all_flags = await repo.list_flags(db)
        assert any(f.id == row.id for f in all_flags)

        by_elder = await repo.list_flags(db, elder_id=eid)
        assert [f.id for f in by_elder] == [row.id]

        open_only = await repo.list_flags(db, status="open")
        assert any(f.id == row.id for f in open_only)
        closed = await repo.list_flags(db, status="closed")
        assert all(f.id != row.id for f in closed)


async def test_list_flags_respects_limit(session_factory):
    cid, eid = await _seed_call_and_elder(session_factory)
    async with session_factory() as db:
        for _ in range(2):
            await repo.create_follow_up_flag(
                db,
                call_id=cid,
                elder_id=eid,
                severity="routine",
                category="other",
                reason=None,
            )
        await db.commit()

        limited = await repo.list_flags(db, elder_id=eid, limit=1)
        assert len(limited) == 1
