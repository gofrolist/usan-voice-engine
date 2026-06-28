"""ORM -> RetellAI KnowledgeBaseResponse (Phase 5). sources omitted unless status=='complete'."""

from __future__ import annotations

from usan_api.compat import ids
from usan_api.compat.schemas.knowledge_bases import KbTextSource, KnowledgeBaseResponse
from usan_api.db.models import KnowledgeBase, KnowledgeBaseSource


def serialize_kb(kb: KnowledgeBase, sources: list[KnowledgeBaseSource]) -> KnowledgeBaseResponse:
    kb_sources: list[KbTextSource] | None = None
    if kb.status == "complete":
        kb_sources = [
            KbTextSource(
                source_id=ids.encode_kb_source_id(s.id),
                title=s.title or "",
                content_url=s.content_url,
            )
            for s in sources
            if s.source_type == "text"
        ]
    return KnowledgeBaseResponse(
        knowledge_base_id=ids.encode_kb_id(kb.id),
        knowledge_base_name=kb.name,
        status=kb.status,
        knowledge_base_sources=kb_sources,
        enable_auto_refresh=kb.enable_auto_refresh,
        max_chunk_size=kb.max_chunk_size,
        min_chunk_size=kb.min_chunk_size,
    )
