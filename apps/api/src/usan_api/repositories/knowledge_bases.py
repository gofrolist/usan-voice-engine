"""knowledge_bases repository (Phase 5). RLS-scoped, org auto-filled. Flush-only — the
caller commits. claim_pending calls the SECURITY DEFINER fn (the only cross-org primitive)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import KnowledgeBase, KnowledgeBaseChunk, KnowledgeBaseSource


async def create_kb(
    db: AsyncSession,
    *,
    name: str,
    max_chunk_size: int,
    min_chunk_size: int,
    enable_auto_refresh: bool,
) -> KnowledgeBase:
    kb = KnowledgeBase(
        name=name,
        status="in_progress",
        max_chunk_size=max_chunk_size,
        min_chunk_size=min_chunk_size,
        enable_auto_refresh=enable_auto_refresh,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    return kb


async def get_kb(db: AsyncSession, kb_id: uuid.UUID) -> KnowledgeBase | None:
    return (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one_or_none()


async def get_existing_kb_ids(db: AsyncSession, kb_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Return the subset of `kb_ids` that exist within the caller's org (RLS-scoped).
    Cross-org ids are simply absent from the result. Empty input -> empty set (no query)."""
    if not kb_ids:
        return set()
    rows = (
        (await db.execute(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))))
        .scalars()
        .all()
    )
    return set(rows)


async def list_kbs(db: AsyncSession) -> list[KnowledgeBase]:
    rows = (
        (await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())))
        .scalars()
        .all()
    )
    return list(rows)


async def delete_kb(db: AsyncSession, kb_id: uuid.UUID) -> bool:
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)),
    )
    return result.rowcount > 0


async def set_status(
    db: AsyncSession,
    kb_id: uuid.UUID,
    status: str,
    *,
    error_detail: str | None = None,
    attempts: int | None = None,
) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = status
    kb.error_detail = error_detail
    kb.claimed_at = None
    if attempts is not None:
        kb.ingestion_attempts = attempts
    await db.flush()


async def mark_retry(db: AsyncSession, kb_id: uuid.UUID, *, attempts: int) -> None:
    """Transient-failure path: return the KB to in_progress (so the claim re-selects it) with
    the incremented attempt counter and the lease cleared (the lease provides backoff before
    the next claim). error_detail is left as-is (informational); status is NOT terminal."""
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = "in_progress"
    kb.claimed_at = None
    kb.ingestion_attempts = attempts
    await db.flush()


async def mark_in_progress(db: AsyncSession, kb_id: uuid.UUID) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = "in_progress"
    kb.claimed_at = None
    # A client adding new sources starts a genuine new attempt cycle — reset the retry counter
    # so prior transient failures don't prematurely exhaust the budget.
    kb.ingestion_attempts = 0
    await db.flush()


async def add_source(
    db: AsyncSession,
    kb_id: uuid.UUID,
    *,
    source_type: str,
    title: str | None,
    content: str,
    content_url: str,
) -> KnowledgeBaseSource:
    src = KnowledgeBaseSource(
        knowledge_base_id=kb_id,
        source_type=source_type,
        title=title,
        content=content,
        content_url=content_url,
    )
    db.add(src)
    await db.flush()
    await db.refresh(src)
    return src


async def get_sources(db: AsyncSession, kb_id: uuid.UUID) -> list[KnowledgeBaseSource]:
    rows = (
        (
            await db.execute(
                select(KnowledgeBaseSource)
                .where(KnowledgeBaseSource.knowledge_base_id == kb_id)
                .order_by(KnowledgeBaseSource.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_sources_for_kbs(
    db: AsyncSession, kb_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[KnowledgeBaseSource]]:
    if not kb_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(KnowledgeBaseSource)
                .where(KnowledgeBaseSource.knowledge_base_id.in_(kb_ids))
                .order_by(KnowledgeBaseSource.created_at)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[KnowledgeBaseSource]] = {kid: [] for kid in kb_ids}
    for r in rows:
        out.setdefault(r.knowledge_base_id, []).append(r)
    return out


async def get_unchunked_sources(db: AsyncSession, kb_id: uuid.UUID) -> list[KnowledgeBaseSource]:
    """Sources with no chunks yet (the ingestion work-list — handles create + add-sources)."""
    sub = select(KnowledgeBaseChunk.source_id).where(
        KnowledgeBaseChunk.source_id == KnowledgeBaseSource.id
    )
    rows = (
        (
            await db.execute(
                select(KnowledgeBaseSource)
                .where(KnowledgeBaseSource.knowledge_base_id == kb_id)
                .where(~sub.exists())
                .order_by(KnowledgeBaseSource.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_source(
    db: AsyncSession, kb_id: uuid.UUID, source_id: uuid.UUID
) -> KnowledgeBaseSource | None:
    return (
        await db.execute(
            select(KnowledgeBaseSource).where(
                KnowledgeBaseSource.id == source_id,
                KnowledgeBaseSource.knowledge_base_id == kb_id,
            )
        )
    ).scalar_one_or_none()


async def delete_source(db: AsyncSession, source_id: uuid.UUID) -> bool:
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(KnowledgeBaseSource).where(KnowledgeBaseSource.id == source_id)),
    )
    return result.rowcount > 0


async def delete_chunks_for_source(db: AsyncSession, source_id: uuid.UUID) -> None:
    await db.execute(delete(KnowledgeBaseChunk).where(KnowledgeBaseChunk.source_id == source_id))
    await db.flush()


async def insert_chunks(
    db: AsyncSession,
    *,
    kb_id: uuid.UUID,
    source_id: uuid.UUID,
    chunks: list[tuple[int, str, list[float]]],
) -> None:
    for idx, content, embedding in chunks:
        db.add(
            KnowledgeBaseChunk(
                knowledge_base_id=kb_id,
                source_id=source_id,
                chunk_index=idx,
                content=content,
                embedding=embedding,
            )
        )
    await db.flush()


async def claim_pending(
    db: AsyncSession, *, limit: int, lease_seconds: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Lease-claim up to `limit` in_progress KBs across ALL orgs via the SECURITY DEFINER fn.
    Returns (kb_id, org_id) pairs. The caller commits, then processes each under its org."""
    rows = (
        await db.execute(
            text("SELECT id, organization_id FROM claim_pending_knowledge_bases(:lim, :lease)"),
            {"lim": limit, "lease": lease_seconds},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


@dataclass(frozen=True)
class ChunkHit:
    knowledge_base_id: uuid.UUID
    content: str
    distance: float


async def search_chunks(
    db: AsyncSession,
    *,
    kb_ids: list[uuid.UUID],
    query_embedding: list[float],
    limit: int,
    max_distance: float,
) -> list[ChunkHit]:
    """RLS-scoped cosine-distance search over the bound KBs' chunks. Returns hits ordered by
    ascending distance, capped at `limit`, with hits above `max_distance` dropped (the relevance
    floor). Empty `kb_ids` -> [] (no query). The embedding binds as a pgvector parameter."""
    if not kb_ids:
        return []
    distance = KnowledgeBaseChunk.embedding.cosine_distance(query_embedding).label("distance")
    rows = (
        await db.execute(
            select(
                KnowledgeBaseChunk.knowledge_base_id,
                KnowledgeBaseChunk.content,
                distance,
            )
            .where(KnowledgeBaseChunk.knowledge_base_id.in_(kb_ids))
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        ChunkHit(knowledge_base_id=r[0], content=r[1], distance=float(r[2]))
        for r in rows
        if float(r[2]) <= max_distance
    ]
