import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_conversation_flows_table_force_rls_and_grant(async_database_url: str) -> None:
    async def _check() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                relrowsecurity, relforcerowsecurity = (
                    await conn.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = 'conversation_flows'"
                        )
                    )
                ).one()
                # Plain per-org table -> FORCE (owner is also policy-bound). This is the
                # OPPOSITE of the 0047 KB tables (ENABLE-only for the cross-org claim fn).
                assert relrowsecurity is True
                assert relforcerowsecurity is True
                policy = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_policy "
                        "WHERE polrelid = 'conversation_flows'::regclass "
                        "AND polname = 'tenant_isolation'"
                    )
                )
                assert policy == 1
                grant_count = await conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.role_table_grants "
                        "WHERE table_name = 'conversation_flows' AND grantee = 'usan_app' "
                        "AND privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')"
                    )
                )
                assert grant_count == 4
        finally:
            await engine.dispose()

    asyncio.run(_check())
