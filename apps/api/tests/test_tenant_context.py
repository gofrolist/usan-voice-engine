import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def test_set_tenant_context_sets_guc(client, async_database_url):
    # `client` populates the app settings env (DATABASE_URL + the required
    # LiveKit/JWT/operator vars) so resolve_default_org_id's get_settings() call
    # succeeds — this test exercises the helper directly, not via a request, but
    # still needs a valid Settings (matches the repo's settings-dependent test pattern).
    async def run() -> str | None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                org_id = await resolve_default_org_id(s)
                await set_tenant_context(s, org_id)
                return (
                    await s.execute(text("SELECT current_setting('app.current_org', true)"))
                ).scalar_one()
        finally:
            await engine.dispose()

    val = asyncio.run(run())
    assert val  # a uuid string, not empty/None
