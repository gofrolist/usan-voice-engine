"""KB ingestion core (Phase 5). Process ONE knowledge base: chunk + embed its un-chunked
sources, store vectors, set status complete/error. The caller (poller) sets the org context
and commits. PHI-safe: errors log type name only — never source/chunk text."""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat.kb_chunking import chunk_text
from usan_api.compat.kb_embeddings import embed_texts
from usan_api.repositories import knowledge_bases as repo
from usan_api.settings import Settings


async def ingest_one_kb(db: AsyncSession, kb_id: uuid.UUID, settings: Settings) -> None:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        return
    if not (settings.kb_embedding_enabled and settings.gcp_project):
        # Flag/project off — leave it claimed-but-pending (status stays in_progress); the
        # next enabled deploy reclaims it after the lease. Never marks complete without embeds.
        logger.bind(kb_id=str(kb_id)).info("KB ingestion skipped (embedding disabled)")
        return
    try:
        for src in await repo.get_unchunked_sources(db, kb_id):
            pieces = chunk_text(src.content, min_size=kb.min_chunk_size, max_size=kb.max_chunk_size)
            await repo.delete_chunks_for_source(db, src.id)  # idempotent re-ingest
            if not pieces:
                continue
            vectors = await embed_texts(pieces, settings)
            await repo.insert_chunks(
                db,
                kb_id=kb_id,
                source_id=src.id,
                chunks=list(zip(range(len(pieces)), pieces, vectors, strict=True)),
            )
        await repo.set_status(db, kb_id, "complete")
    except Exception as exc:  # PHI-safe: type name only
        logger.bind(kb_id=str(kb_id), exc_type=type(exc).__name__).error(
            "KB ingestion failed kb={kb_id} exc={exc_type}"
        )
        await repo.set_status(db, kb_id, "error", error_detail=type(exc).__name__)
    await db.flush()
