"""Shared KB test helpers — superuser-level seed/teardown utilities used across test modules."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


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
