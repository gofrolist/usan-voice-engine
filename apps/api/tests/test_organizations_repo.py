import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories.organizations import get_org_by_slug


def test_default_org_seeded(async_database_url):
    async def run():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await get_org_by_slug(s, "usan")
        finally:
            await engine.dispose()

    org = asyncio.run(run())
    assert org is not None
    assert org.slug == "usan"
    assert org.name == "USAN Retirement"
