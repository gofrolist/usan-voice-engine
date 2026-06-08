import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import AdminRole
from usan_api.repositories import admin_users as repo


def _factory(async_database_url: str):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _truncate(async_database_url: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE admin_users RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


def test_add_get_remove_round_trip(async_database_url):
    async def _run():
        await _truncate(async_database_url)
        engine, factory = _factory(async_database_url)
        try:
            async with factory() as db:
                await repo.add_admin_user(
                    db, email="Alice@Example.com", role=AdminRole.ADMIN, added_by="me@x.com"
                )
                await db.commit()
            async with factory() as db:
                # email is normalized to lowercase on write.
                u = await repo.get_admin_user(db, "alice@example.com")
                assert u is not None
                assert u.role is AdminRole.ADMIN
                assert u.added_by == "me@x.com"
                users = await repo.list_admin_users(db)
                assert [x.email for x in users] == ["alice@example.com"]
            async with factory() as db:
                removed = await repo.remove_admin_user(db, "alice@example.com")
                await db.commit()
                assert removed is True
            async with factory() as db:
                assert await repo.get_admin_user(db, "alice@example.com") is None
                assert await repo.remove_admin_user(db, "alice@example.com") is False
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_seed_bootstrap_is_idempotent(async_database_url):
    async def _run():
        await _truncate(async_database_url)
        engine, factory = _factory(async_database_url)
        try:
            async with factory() as db:
                n1 = await repo.seed_bootstrap(db, ["a@x.com", "b@x.com"])
                await db.commit()
                assert n1 == 2
            async with factory() as db:
                # Re-seeding inserts nothing new and never errors on existing rows.
                n2 = await repo.seed_bootstrap(db, ["a@x.com", "b@x.com", "c@x.com"])
                await db.commit()
                assert n2 == 1
            async with factory() as db:
                emails = {u.email for u in await repo.list_admin_users(db)}
                assert emails == {"a@x.com", "b@x.com", "c@x.com"}
        finally:
            await engine.dispose()

    asyncio.run(_run())
