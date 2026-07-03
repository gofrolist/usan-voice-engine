"""Native admin knowledge-bases API (text-only v1). RLS-scoped, org-admin self-service:
router-level session gate allows any VIEWER+; writes add require_admin_role(ADMIN). Raw
UUIDs (not compat kb_ tokens). Reuses the KB repo + the shared add_text_sources helper so
ingestion (the running poller) needs no extra wiring. Source content is never echoed."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.compat.kb_sources import TextSource, add_text_sources
from usan_api.db.base import AdminRole
from usan_api.db.models import KnowledgeBase
from usan_api.repositories import admin_audit
from usan_api.repositories import knowledge_bases as repo
from usan_api.schemas.admin_knowledge_bases import (
    KbCreate,
    KbDetail,
    KbSourceCreate,
    KbSourceOut,
    KbSummary,
)

# Native KB defaults — mirror ParsedKbCreate's compat defaults so a KB created here
# chunks identically to a compat KB.
_DEFAULT_MAX_CHUNK = 2000
_DEFAULT_MIN_CHUNK = 400

router = APIRouter(
    prefix="/v1/admin/knowledge-bases",
    tags=["admin-knowledge-bases"],
    dependencies=[Depends(require_admin_session)],
)


async def _detail(db: AsyncSession, kb: KnowledgeBase) -> KbDetail:
    rows = await repo.get_sources_with_status(db, kb.id)
    return KbDetail(
        id=kb.id,
        name=kb.name,
        status=kb.status,
        error_detail=kb.error_detail,
        sources=[
            KbSourceOut(
                id=s.id,
                title=s.title,
                status="embedded" if has_chunks else "pending",
                created_at=s.created_at,
            )
            for s, has_chunks in rows
        ],
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


@router.get("", response_model=list[KbSummary])
async def list_knowledge_bases(db: AsyncSession = Depends(get_tenant_db)) -> list[KbSummary]:
    kbs = await repo.list_kbs(db)
    by_kb = await repo.get_sources_for_kbs(db, [k.id for k in kbs])
    return [
        KbSummary(
            id=k.id,
            name=k.name,
            status=k.status,
            source_count=len(by_kb.get(k.id, [])),
            updated_at=k.updated_at,
        )
        for k in kbs
    ]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=KbDetail)
async def create_knowledge_base(
    body: KbCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> KbDetail:
    kb = await repo.create_kb(
        db,
        name=body.name,
        max_chunk_size=_DEFAULT_MAX_CHUNK,
        min_chunk_size=_DEFAULT_MIN_CHUNK,
        enable_auto_refresh=False,
    )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.create",
        entity_type="knowledge_base",
        entity_id=str(kb.id),
    )
    await db.commit()
    await db.refresh(kb)
    return await _detail(db, kb)


@router.get("/{kb_id}", response_model=KbDetail)
async def get_knowledge_base(
    kb_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> KbDetail:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return await _detail(db, kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    if not await repo.delete_kb(db, kb_id):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.delete",
        entity_type="knowledge_base",
        entity_id=str(kb_id),
    )
    await db.commit()


@router.post("/{kb_id}/sources", status_code=status.HTTP_201_CREATED, response_model=KbDetail)
async def add_knowledge_base_source(
    kb_id: uuid.UUID,
    body: KbSourceCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> KbDetail:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    created = await add_text_sources(db, kb_id, [TextSource(title=body.title, text=body.text)])
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.add_source",
        entity_type="knowledge_base_source",
        entity_id=str(created[0]),
        detail={"knowledge_base_id": str(kb_id)},
    )
    await db.commit()
    # refresh the instance already in hand (mark_in_progress mutated it) rather than re-query.
    await db.refresh(kb)
    return await _detail(db, kb)


@router.delete("/{kb_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base_source(
    kb_id: uuid.UUID,
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    if await repo.get_source(db, kb_id, source_id) is None:
        raise HTTPException(status_code=404, detail="source not found")
    if not await repo.delete_source(db, source_id):
        # Pre-check passed but the row is gone (concurrent delete): 404, no phantom audit row.
        raise HTTPException(status_code=404, detail="source not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.delete_source",
        entity_type="knowledge_base_source",
        entity_id=str(source_id),
        detail={"knowledge_base_id": str(kb_id)},
    )
    await db.commit()
