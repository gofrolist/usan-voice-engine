"""RetellAI-compat conversation-flow-component CRUD (Phase 6b): create/get/update/delete/list.

The component body is persisted (JSONB) and echoed conformantly but NOT executed at call/chat
time (persisted-not-honored — the DAG runtime is a later sub-phase). A component is a standalone
entity (its own conversation_flow_components table). The session does not autocommit; each
mutation commits explicitly. Delete is a plain soft-delete: the oracle's "local copies for
linked flows" fan-out is not backed (nothing links components to flows at rest).
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.conversation_flow_component import (
    CreateConversationFlowComponentRequest,
    UpdateConversationFlowComponentRequest,
    serialize_component,
)
from usan_api.repositories import conversation_flow_components as components_repo

router = APIRouter(tags=["compat-conversation-flow-components"])

# Server-generated response fields — never stored inside the component config. A client can inject
# them via extra='allow', but serialize_component always derives them from the ORM columns, so we
# drop them before persisting (defense-in-depth against a future reader of row.config[...]).
_SERVER_KEYS = ("conversation_flow_component_id", "user_modified_timestamp")


def _strip_server_keys(config: dict[str, Any]) -> dict[str, Any]:
    for _k in _SERVER_KEYS:
        config.pop(_k, None)
    return config


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the component config (it can carry prompts).
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat conversation-flow-component op={op}")


def _provided(
    model: CreateConversationFlowComponentRequest | UpdateConversationFlowComponentRequest,
) -> dict[str, Any]:
    # Drop null-valued keys (declared and extra='allow') so we store/merge only real values.
    return {k: v for k, v in model.model_dump().items() if v is not None}


@router.post("/create-conversation-flow-component", status_code=status.HTTP_201_CREATED)
async def create_conversation_flow_component(
    body: CreateConversationFlowComponentRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    config = _strip_server_keys(_provided(body))
    row = await components_repo.create(db, config=config)
    await db.commit()
    _audit(request, "create-conversation-flow-component")
    return serialize_component(row)


@router.get("/get-conversation-flow-component/{conversation_flow_component_id}")
async def get_conversation_flow_component(
    conversation_flow_component_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    row = await components_repo.get(db, component_id)
    if row is None:
        raise CompatError(404, "conversation flow component not found")
    _audit(request, "get-conversation-flow-component")
    return serialize_component(row)


@router.patch("/update-conversation-flow-component/{conversation_flow_component_id}")
async def update_conversation_flow_component(
    conversation_flow_component_id: str,
    body: UpdateConversationFlowComponentRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    row = await components_repo.get(db, component_id)
    if row is None:
        raise CompatError(404, "conversation flow component not found")
    # Top-level shallow merge: a sent non-null field overwrites; a sent explicit null CLEARS the
    # field (removed -> omitted from the echo, matching the oracle's omit-nulls responses); an
    # omitted field is preserved. body.model_dump() carries the extra='allow' fields incl nulls.
    merged = dict(row.config)
    for _key, _value in body.model_dump().items():
        if _value is None:
            merged.pop(_key, None)
        else:
            merged[_key] = _value
    _strip_server_keys(merged)
    updated = await components_repo.update(db, component_id, config=merged)
    if updated is None:
        raise CompatError(404, "conversation flow component not found")
    await db.commit()
    _audit(request, "update-conversation-flow-component")
    return serialize_component(updated)


@router.delete(
    "/delete-conversation-flow-component/{conversation_flow_component_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation_flow_component(
    conversation_flow_component_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    # Plain soft-delete. The oracle's "creates local copies for all linked conversation flows"
    # fan-out is not backed (nothing links components to flows at rest) — documented, not faked.
    if not await components_repo.archive(db, component_id):
        raise CompatError(404, "conversation flow component not found")
    await db.commit()
    _audit(request, "delete-conversation-flow-component")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v2/list-conversation-flow-components")
async def list_conversation_flow_components(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after = None
    if pagination_key:
        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after = ids.decode_conversation_flow_component_cursor(pagination_key)
    rows = await components_repo.list_components(
        db, limit=limit, descending=(sort_order != "ascending"), after=after
    )
    _audit(request, "list-conversation-flow-components")
    has_more = len(rows) > limit
    page = rows[:limit]
    out: dict[str, Any] = {
        "items": [serialize_component(r) for r in page],
        "has_more": has_more,
    }
    if has_more:
        out["pagination_key"] = ids.encode_conversation_flow_component_cursor(
            page[-1].created_at, page[-1].id
        )
    return out
