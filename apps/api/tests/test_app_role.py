import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_usan_app_is_not_superuser_and_not_bypassrls(async_database_url):
    async def check() -> tuple[bool, bool]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'usan_app'"
                        )
                    )
                ).one()
            return bool(row[0]), bool(row[1])
        finally:
            await engine.dispose()

    rolsuper, rolbypassrls = asyncio.run(check())
    assert rolsuper is False
    assert rolbypassrls is False
