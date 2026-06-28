import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_kb_tables_and_extension_and_rls(async_database_url: str) -> None:
    async def _check() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                ext = await conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
                assert ext == 1
                for t in ("knowledge_bases", "knowledge_base_sources", "knowledge_base_chunks"):
                    relrowsecurity, relforcerowsecurity = (
                        await conn.execute(
                            text(
                                "SELECT relrowsecurity, relforcerowsecurity "
                                "FROM pg_class WHERE relname = :t"
                            ),
                            {"t": t},
                        )
                    ).one()
                    assert relrowsecurity is True, t
                    assert relforcerowsecurity is True, t
                hnsw = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE indexname = 'ix_knowledge_base_chunks_embedding_hnsw'"
                    )
                )
                assert hnsw == 1
                fn = await conn.scalar(
                    text("SELECT 1 FROM pg_proc WHERE proname = 'claim_pending_knowledge_bases'")
                )
                assert fn == 1
                grant = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.role_table_grants "
                        "WHERE table_name = 'knowledge_bases' AND grantee = 'usan_app' "
                        "AND privilege_type = 'INSERT'"
                    )
                )
                assert grant == 1
        finally:
            await engine.dispose()

    asyncio.run(_check())
