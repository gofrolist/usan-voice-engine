import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import settings as settings_mod
from usan_api.compat import kb_ingestion_poller
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def _on():
    return settings_mod.get_settings().model_copy(
        update={"kb_embedding_enabled": True, "gcp_project": "p", "kb_ingestion_batch_size": 50}
    )


@pytest.mark.asyncio
async def test_poll_processes_two_orgs(
    app_session, app_async_database_url, async_database_url, mock_embed
) -> None:
    # Truncate all 3 KB tables so leftover in_progress rows from other tests don't skew the count.
    eng0 = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with eng0.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE knowledge_base_chunks, knowledge_base_sources,"
                    " knowledge_bases CASCADE"
                )
            )
    finally:
        await eng0.dispose()

    # Org A pending KB (default org), seeded under RLS via app_session.
    org_a = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org_a)
    kb_a = await repo.create_kb(
        app_session, name="a", max_chunk_size=2000, min_chunk_size=2, enable_auto_refresh=False
    )
    await repo.add_source(
        app_session, kb_a.id, source_type="text", title="t", content="aaa bbb", content_url="u"
    )
    await app_session.commit()

    # Org B + its pending KB + source, seeded directly (superuser bypasses RLS).
    org_b = uuid.uuid4()
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :s, :n)"),
                {"id": str(org_b), "s": f"org-{org_b.hex[:8]}", "n": "Org B"},
            )
            kb_b = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases (organization_id, name, status, "
                        "max_chunk_size, min_chunk_size) VALUES (:o,'b','in_progress',2000,2) "
                        "RETURNING id"
                    ),
                    {"o": str(org_b)},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO knowledge_base_sources (organization_id, knowledge_base_id, "
                    "source_type, title, content, content_url) "
                    "VALUES (:o, :kb, 'text', 't', 'ccc ddd', 'u')"
                ),
                {"o": str(org_b), "kb": str(kb_b)},
            )
    finally:
        await engine.dispose()

    # Run the poller against a usan_app factory (RLS-subject — proves the cross-org claim).
    factory = async_sessionmaker(
        create_async_engine(app_async_database_url, poolclass=NullPool), expire_on_commit=False
    )
    try:
        processed = await kb_ingestion_poller.poll_once(factory, _on())
        # Now exactly 2: only the 2 seeded KBs exist (table was truncated above).
        assert processed == 2

        # Both KBs reached complete (verify via the superuser engine).
        eng = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with eng.connect() as conn:
                for kid in (kb_a.id, kb_b):
                    st = await conn.scalar(
                        text("SELECT status FROM knowledge_bases WHERE id = :id"), {"id": str(kid)}
                    )
                    assert st == "complete", kid
        finally:
            await eng.dispose()
    finally:
        # Clean up org_b rows so the leaked org doesn't affect other tests.
        eng_cleanup = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with eng_cleanup.begin() as conn:
                await conn.execute(
                    text("DELETE FROM knowledge_bases WHERE organization_id = :o"),
                    {"o": str(org_b)},
                )
                await conn.execute(
                    text("DELETE FROM organizations WHERE id = :o"), {"o": str(org_b)}
                )
        finally:
            await eng_cleanup.dispose()
