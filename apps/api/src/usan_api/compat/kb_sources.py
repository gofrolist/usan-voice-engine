"""Shared KB text-source persistence used by BOTH the RetellAI-compat kb_service and
the native admin knowledge-bases router. Single source of truth for how a text source
is stored and how adding one (re)triggers ingestion — the two surfaces cannot drift."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.repositories import knowledge_bases as repo


@dataclass(frozen=True)
class TextSource:
    title: str
    text: str


def _content_url(source_id: uuid.UUID) -> str:
    # Internal reference; content lives in the DB and is not publicly served in v1.
    return f"https://knowledge-base.internal/source/{ids.encode_kb_source_id(source_id)}"


async def add_text_sources(db: AsyncSession, kb_id: uuid.UUID, texts: list[TextSource]) -> None:
    """Persist each text source under ``kb_id`` and, when any were added, reset the KB to
    ``in_progress`` so the ingestion poller re-claims it and embeds the new sources.
    Flush-only — the caller commits. Empty ``texts`` is a no-op (KB status untouched)."""
    for t in texts:
        src = await repo.add_source(
            db, kb_id, source_type="text", title=t.title, content=t.text, content_url=""
        )
        src.content_url = _content_url(src.id)
    await db.flush()
    if texts:
        # New sources are un-chunked; returning the KB to in_progress re-enters the claim.
        await repo.mark_in_progress(db, kb_id)
