"""RetellAI knowledge-base compat routes (Phase 5). Exact paths + codes (201/201/200/200/
204/200). multipart create/add-sources: list fields arrive as JSON-string blobs (retell-sdk
_serialize_multipartform); files as 'knowledge_base_files[]'. response_model_exclude_none."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request, Response, status
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids, kb_service
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.kb_serializer import serialize_kb
from usan_api.compat.schemas.knowledge_bases import (
    KbTextInput,
    KnowledgeBaseResponse,
    ParsedKbAddSources,
    ParsedKbCreate,
)
from usan_api.repositories import knowledge_bases as repo

router = APIRouter(tags=["compat-knowledge-bases"])


def _audit(request: Request, op: str, kb_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, knowledge_base_id=kb_id).info("compat kb op={op}")


def _parse_texts(raw: str | None) -> list[KbTextInput]:
    if raw is None:
        return []
    try:
        items = json.loads(raw)
        return [KbTextInput.model_validate(i) for i in items]
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise CompatError(422, "invalid knowledge_base_texts") from exc


def _has_urls(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        return bool(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise CompatError(422, "invalid knowledge_base_urls") from exc


async def _has_files(request: Request) -> bool:
    form = await request.form()
    return bool(form.getlist("knowledge_base_files[]"))


@router.post(
    "/create-knowledge-base",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def create_knowledge_base(
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    knowledge_base_name: str = Form(...),
    knowledge_base_texts: str | None = Form(None),
    knowledge_base_urls: str | None = Form(None),
    enable_auto_refresh: bool = Form(False),
    max_chunk_size: int = Form(2000),
    min_chunk_size: int = Form(400),
) -> KnowledgeBaseResponse:
    parsed = ParsedKbCreate(
        name=knowledge_base_name,
        texts=_parse_texts(knowledge_base_texts),
        has_files=await _has_files(request),
        has_urls=_has_urls(knowledge_base_urls),
        enable_auto_refresh=enable_auto_refresh,
        max_chunk_size=max_chunk_size,
        min_chunk_size=min_chunk_size,
    )
    kb = await kb_service.create_kb(db, parsed)
    _audit(request, "create-knowledge-base", ids.encode_kb_id(kb.id))
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.post(
    "/add-knowledge-base-sources/{knowledge_base_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def add_knowledge_base_sources(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    knowledge_base_texts: str | None = Form(None),
    knowledge_base_urls: str | None = Form(None),
) -> KnowledgeBaseResponse:
    parsed = ParsedKbAddSources(
        texts=_parse_texts(knowledge_base_texts),
        has_files=await _has_files(request),
        has_urls=_has_urls(knowledge_base_urls),
    )
    kb = await kb_service.add_sources(db, knowledge_base_id, parsed)
    _audit(request, "add-knowledge-base-sources", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.get(
    "/get-knowledge-base/{knowledge_base_id}",
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def get_knowledge_base(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> KnowledgeBaseResponse:
    kb = await kb_service.get_kb(db, knowledge_base_id)
    _audit(request, "get-knowledge-base", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.get(
    "/list-knowledge-bases",
    response_model=list[KnowledgeBaseResponse],
    response_model_exclude_none=True,
)
async def list_knowledge_bases(
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> list[KnowledgeBaseResponse]:
    kbs = await kb_service.list_kbs(db)
    sources = await repo.get_sources_for_kbs(db, [k.id for k in kbs])
    _audit(request, "list-knowledge-bases")
    return [serialize_kb(k, sources.get(k.id, [])) for k in kbs]


@router.delete("/delete-knowledge-base/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await kb_service.delete_kb(db, knowledge_base_id)
    _audit(request, "delete-knowledge-base", knowledge_base_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/delete-knowledge-base-source/{knowledge_base_id}/source/{source_id}",
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def delete_knowledge_base_source(
    knowledge_base_id: str,
    source_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> KnowledgeBaseResponse:
    kb = await kb_service.delete_source(db, knowledge_base_id, source_id)
    _audit(request, "delete-knowledge-base-source", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))
