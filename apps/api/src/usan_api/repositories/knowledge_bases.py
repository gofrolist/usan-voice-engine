"""knowledge_bases repository (Phase 5). RLS-scoped, org auto-filled. Flush-only — the
caller commits. claim_pending calls the SECURITY DEFINER fn (the only cross-org primitive)."""

from __future__ import annotations

import uuid
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
    db: AsyncSession, kb_id: uuid.UUID, status: str, *, error_detail: str | None = None
) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = status
    kb.error_detail = error_detail
    kb.claimed_at = None
    await db.flush()


async def mark_in_progress(db: AsyncSession, kb_id: uuid.UUID) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = "in_progress"
    kb.claimed_at = None
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
