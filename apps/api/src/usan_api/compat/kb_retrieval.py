"""KB text-RAG retrieval orchestration (Phase 5b). Gates, embeds the query, runs the RLS-scoped
vector search, and assembles a bounded context block. PHI/secret-safe: logs counts + bucketed
distances only — never chunk text, query text, titles, or ids. Embed/search failures propagate;
the chat-service caller wraps this and degrades to a no-context reply."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import usan_api.compat.kb_embeddings as _kb_embeddings
import usan_api.repositories.knowledge_bases as _kb_repo
from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.settings import Settings

# Module-level aliases so monkeypatch can intercept them in tests.
embed_query = _kb_embeddings.embed_query
search_chunks = _kb_repo.search_chunks


@dataclass(frozen=True)
class RetrievedContext:
    text: str
    hit_count: int


_EMPTY = RetrievedContext("", 0)


def _assemble(contents: list[str], max_chars: int) -> str:
    """Join chunk contents with a blank-line separator, stopping before exceeding max_chars.
    A single first chunk longer than the cap is truncated (a hit is never silently dropped)."""
    parts: list[str] = []
    total = 0
    for piece in contents:
        if not parts and len(piece) > max_chars:
            parts.append(piece[:max_chars])
            break
        sep = 2 if parts else 0  # cost of the "\n\n" join
        if total + sep + len(piece) > max_chars:
            break
        parts.append(piece)
        total += sep + len(piece)
    return "\n\n".join(parts)


async def retrieve_context(
    db: AsyncSession, settings: Settings, *, kb_ids: list[str], query: str, enabled: bool
) -> RetrievedContext:
    # `enabled` is the CHANNEL's gate: chat passes settings.kb_retrieval_enabled, voice
    # passes settings.kb_retrieval_voice_enabled. gcp_project stays the egress hard-gate.
    if not enabled or not settings.gcp_project or not kb_ids:
        return _EMPTY
    if not query.strip():
        return _EMPTY
    kb_uuids = []
    for token in kb_ids:
        try:
            kb_uuids.append(ids.decode_kb_id(token))
        except CompatError:
            continue  # defensive: ids were validated at bind, but never 500 here
    if not kb_uuids:
        return _EMPTY
    vector = await embed_query(query, settings)
    hits = await search_chunks(
        db,
        kb_ids=kb_uuids,
        query_embedding=vector,
        limit=settings.kb_retrieval_top_k,
        max_distance=settings.kb_retrieval_max_distance,
    )
    text = _assemble([h.content for h in hits], settings.kb_retrieval_max_context_chars)
    nearest = round(hits[0].distance, 2) if hits else None
    logger.bind(kb_count=len(kb_uuids), hits=len(hits), nearest=nearest).debug(
        "kb retrieval kb_count={kb_count} hits={hits} nearest={nearest}"
    )
    return RetrievedContext(text=text, hit_count=len(hits))
