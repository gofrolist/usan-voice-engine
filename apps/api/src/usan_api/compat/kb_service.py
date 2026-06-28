"""KB compat service (Phase 5). Validation + persistence; returns ORM (router serializes).
Every mutation commits (get_compat_db does not autocommit). content_url is an internal
reference (content lives in the DB; not publicly served in v1 — documented posture)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.knowledge_bases import (
    KbTextInput,
    ParsedKbAddSources,
    ParsedKbCreate,
)
from usan_api.db.models import KnowledgeBase
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import set_tenant_context

_NAME_MAX = 40
_CHUNK_MAX_LO, _CHUNK_MAX_HI = 600, 6000
_CHUNK_MIN_LO, _CHUNK_MIN_HI = 200, 2000


def _content_url(source_id: uuid.UUID) -> str:
    # Internal reference; not publicly served in v1 (documented posture).
    return f"https://knowledge-base.internal/source/{ids.encode_kb_source_id(source_id)}"


def _reject_unsupported_sources(has_files: bool, has_urls: bool) -> None:
    if has_files or has_urls:
        raise CompatError(422, "only text sources are supported")


def _validate_create(p: ParsedKbCreate) -> None:
    if not p.name or len(p.name) >= _NAME_MAX:
        raise CompatError(422, "invalid knowledge_base_name")
    if not (_CHUNK_MAX_LO <= p.max_chunk_size <= _CHUNK_MAX_HI):
        raise CompatError(422, "invalid max_chunk_size")
    if not (_CHUNK_MIN_LO <= p.min_chunk_size <= _CHUNK_MIN_HI):
        raise CompatError(422, "invalid min_chunk_size")
    if p.min_chunk_size >= p.max_chunk_size:
        raise CompatError(422, "min_chunk_size must be < max_chunk_size")
    _reject_unsupported_sources(p.has_files, p.has_urls)


async def _persist_texts(db: AsyncSession, kb_id: uuid.UUID, texts: list[KbTextInput]) -> None:
    for t in texts:
        src = await repo.add_source(
            db, kb_id, source_type="text", title=t.title, content=t.text, content_url=""
        )
        src.content_url = _content_url(src.id)
    await db.flush()


async def create_kb(db: AsyncSession, parsed: ParsedKbCreate) -> KnowledgeBase:
    _validate_create(parsed)
    kb = await repo.create_kb(
        db,
        name=parsed.name,
        max_chunk_size=parsed.max_chunk_size,
        min_chunk_size=parsed.min_chunk_size,
        enable_auto_refresh=parsed.enable_auto_refresh,
    )
    await _persist_texts(db, kb.id, parsed.texts)
    await db.commit()
    return kb


async def add_sources(
    db: AsyncSession, kb_id_token: str, parsed: ParsedKbAddSources
) -> KnowledgeBase:
    _reject_unsupported_sources(parsed.has_files, parsed.has_urls)
    kb_id = ids.decode_kb_id(kb_id_token)
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise CompatError(404, "knowledge base not found")
    org_id = kb.organization_id
    await _persist_texts(db, kb_id, parsed.texts)
    if parsed.texts:
        await repo.mark_in_progress(db, kb_id)  # new sources are un-chunked -> re-claimed
    await db.commit()
    # In prod the get_compat_db after_begin listener already re-applies app.current_org on the
    # new post-commit transaction, so the SELECT below is RLS-scoped. This explicit re-apply is
    # for the app_session-based service tests (which have no such listener) and is harmless
    # defense-in-depth in prod.
    await set_tenant_context(db, org_id)
    kb2 = await repo.get_kb(db, kb_id)
    assert kb2 is not None
    return kb2


async def get_kb(db: AsyncSession, kb_id_token: str) -> KnowledgeBase:
    kb = await repo.get_kb(db, ids.decode_kb_id(kb_id_token))
    if kb is None:
        raise CompatError(404, "knowledge base not found")
    return kb


async def list_kbs(db: AsyncSession) -> list[KnowledgeBase]:
    return await repo.list_kbs(db)


async def delete_kb(db: AsyncSession, kb_id_token: str) -> None:
    if not await repo.delete_kb(db, ids.decode_kb_id(kb_id_token)):
        raise CompatError(404, "knowledge base not found")
    await db.commit()


async def delete_source(db: AsyncSession, kb_id_token: str, source_id_token: str) -> KnowledgeBase:
    kb_id = ids.decode_kb_id(kb_id_token)
    source_id = ids.decode_kb_source_id(source_id_token)
    kb = await repo.get_kb(db, kb_id)
    if kb is None or await repo.get_source(db, kb_id, source_id) is None:
        raise CompatError(404, "source not found")
    org_id = kb.organization_id
    await repo.delete_source(db, source_id)
    await db.commit()
    # In prod the get_compat_db after_begin listener already re-applies app.current_org on the
    # new post-commit transaction, so the SELECT below is RLS-scoped. This explicit re-apply is
    # for the app_session-based service tests (which have no such listener) and is harmless
    # defense-in-depth in prod.
    await set_tenant_context(db, org_id)
    kb2 = await repo.get_kb(db, kb_id)
    assert kb2 is not None
    return kb2
