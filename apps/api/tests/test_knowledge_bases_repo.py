import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import set_tenant_context


async def _seed_kb_for_org(super_url: str, org_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert a KB directly (superuser bypasses RLS) for an arbitrary org; returns its id."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases "
                        "(organization_id, name, status, max_chunk_size, min_chunk_size) "
                        "VALUES (:org, :name, 'in_progress', 2000, 400) RETURNING id"
                    ),
                    {"org": str(org_id), "name": name},
                )
            ).one()
            return row[0]
    finally:
        await engine.dispose()


async def _delete_kbs_for_org(super_url: str, org_id: uuid.UUID) -> None:
    """Delete all KB rows for an org (superuser) so org teardown FK constraint is satisfied."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM knowledge_bases WHERE organization_id = :org"),
                {"org": str(org_id)},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_get_list_delete(two_orgs, app_session) -> None:
    org_a, _org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    kb = await repo.create_kb(
        app_session, name="kb1", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    # Verify within the same (uncommitted) transaction so rollback cleans up.
    got = await repo.get_kb(app_session, kb.id)
    assert got is not None
    assert got.name == "kb1"
    assert got.status == "in_progress"
    assert kb.id in {k.id for k in await repo.list_kbs(app_session)}
    deleted = await repo.delete_kb(app_session, kb.id)
    await app_session.flush()
    assert deleted is True


@pytest.mark.asyncio
async def test_chunk_vector_roundtrip_and_unchunked(two_orgs, app_session) -> None:
    org_a, _org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    kb = await repo.create_kb(
        app_session, name="kb", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    src = await repo.add_source(
        app_session, kb.id, source_type="text", title="t", content="body", content_url="u"
    )
    assert [s.id for s in await repo.get_unchunked_sources(app_session, kb.id)] == [src.id]
    await repo.insert_chunks(
        app_session, kb_id=kb.id, source_id=src.id, chunks=[(0, "body", [0.1] * 768)]
    )
    assert await repo.get_unchunked_sources(app_session, kb.id) == []


@pytest.mark.asyncio
async def test_cross_org_isolation_and_claim(two_orgs, app_session, async_database_url) -> None:
    # NOTE: CI's `usan` role is a SUPERUSER, so the claim fn would see all orgs even under FORCE
    # RLS — this behavioral test therefore CANNOT by itself catch the prod FORCE-RLS claim defect
    # (prod `usan` is non-superuser, NO BYPASSRLS, so FORCE would silently scope the claim to the
    # poller's default org). The `relforcerowsecurity=False` assertion in
    # test_knowledge_bases_migration is the prod-condition regression gate.
    org_a, org_b = two_orgs
    # Seed a pending KB in org B directly (superuser bypasses RLS).
    kb_b = await _seed_kb_for_org(async_database_url, org_b, "kb-b")
    try:
        await set_tenant_context(app_session, org_a)
        # Org A cannot see org B's KB (RLS) — proves the cross-org SELECT is fail-closed.
        assert await repo.get_kb(app_session, kb_b) is None
        # The SECURITY DEFINER claim DOES see org B's pending KB even under org-A context.
        claimed = await repo.claim_pending(app_session, limit=50, lease_seconds=300)
        await app_session.commit()
        assert org_b in {org for (_kid, org) in claimed}
        assert kb_b in {kid for (kid, _org) in claimed}
    finally:
        # Delete KB rows for org B before two_orgs teardown tries to delete the org
        # (knowledge_bases.organization_id FK has no ON DELETE CASCADE).
        await _delete_kbs_for_org(async_database_url, org_b)
