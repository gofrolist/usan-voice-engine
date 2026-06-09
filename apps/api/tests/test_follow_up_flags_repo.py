"""follow_up_flags repository: create + filtered list (mirrors wellness repo)."""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import follow_up_flags as repo


async def _seed_call_and_elder(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_async_engine(url, poolclass=NullPool)
    eid, cid = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (:e, 'Flag Elder', :p, 'UTC')"
                ),
                {"e": str(eid), "p": f"+1555{str(eid.int)[:7]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (:c, :e, 'outbound', 'completed')"
                ),
                {"c": str(cid), "e": str(eid)},
            )
    finally:
        await engine.dispose()
    return cid, eid


def test_create_and_list_follow_up_flag(async_database_url):
    cid, eid = asyncio.run(_seed_call_and_elder(async_database_url))
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run():
        async with factory() as db:
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

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())
