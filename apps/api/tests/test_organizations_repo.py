import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories.organizations import get_org_by_slug, get_orgs_by_ids


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


def test_get_orgs_by_ids_batches_and_dedupes(async_database_url, two_orgs):
    org_a, org_b = two_orgs
    missing = uuid.uuid4()

    async def run():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                # Duplicate id + an unknown id exercise de-dupe and the absent-row path.
                return await get_orgs_by_ids(s, [org_a, org_b, org_a, missing])
        finally:
            await engine.dispose()

    by_id = asyncio.run(run())
    assert set(by_id) == {org_a, org_b}
    assert by_id[org_a].id == org_a
    assert by_id[org_b].id == org_b


def test_get_orgs_by_ids_empty(async_database_url):
    async def run():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await get_orgs_by_ids(s, [])
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == {}
