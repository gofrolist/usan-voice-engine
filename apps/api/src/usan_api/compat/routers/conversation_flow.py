"""RetellAI-compat conversation-flow CRUD (Phase 6a): create/get/update/delete/list.

The flow DAG is persisted (JSONB) and echoed conformantly but NOT executed at call/chat time
(persisted-not-honored — the DAG runtime is a later sub-phase). A flow is a standalone entity
(its own conversation_flows table), referenced by agents in 6c. The session does not autocommit;
each mutation commits explicitly. See docs/deployment/conversation-flows.md.
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
from usan_api.compat.schemas.conversation_flow import (
    CreateConversationFlowRequest,
    UpdateConversationFlowRequest,
    serialize_flow,
)
from usan_api.repositories import conversation_flows as flows_repo

router = APIRouter(tags=["compat-conversation-flows"])

# Server-generated response fields — never stored inside the flow config. A client can inject
# them via extra='allow', but serialize_flow always derives them from the ORM columns, so we
# drop them before persisting (defense-in-depth against a future reader of row.config[...]).
_SERVER_KEYS = ("conversation_flow_id", "version", "last_modification_timestamp")


def _strip_server_keys(config: dict[str, Any]) -> dict[str, Any]:
    for _k in _SERVER_KEYS:
        config.pop(_k, None)
    return config


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the flow config (it can carry prompts).
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat conversation-flow op={op}")


def _provided(
    model: CreateConversationFlowRequest | UpdateConversationFlowRequest,
) -> dict[str, Any]:
    # Drop null-valued keys (declared and extra='allow') so we store/merge only real values.
    return {k: v for k, v in model.model_dump().items() if v is not None}


@router.post("/create-conversation-flow", status_code=status.HTTP_201_CREATED)
async def create_conversation_flow(
    body: CreateConversationFlowRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    config = _strip_server_keys(_provided(body))
    row = await flows_repo.create(db, config=config, version=0)
    await db.commit()
    _audit(request, "create-conversation-flow")
    return serialize_flow(row)


@router.get("/get-conversation-flow/{conversation_flow_id}")
async def get_conversation_flow(
    conversation_flow_id: str,
    request: Request,
    version: int | None = Query(default=None),  # accepted, ignored (current-only)
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    row = await flows_repo.get(db, flow_id)
    if row is None:
        raise CompatError(404, "conversation flow not found")
    _audit(request, "get-conversation-flow")
    return serialize_flow(row)


@router.patch("/update-conversation-flow/{conversation_flow_id}")
async def update_conversation_flow(
    conversation_flow_id: str,
    body: UpdateConversationFlowRequest,
    request: Request,
    version: int | None = Query(default=None),  # accepted, ignored
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    row = await flows_repo.get(db, flow_id)
    if row is None:
        raise CompatError(404, "conversation flow not found")
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
    updated = await flows_repo.update(db, flow_id, config=merged, version=row.version + 1)
    if updated is None:
        raise CompatError(404, "conversation flow not found")
    await db.commit()
    _audit(request, "update-conversation-flow")
    return serialize_flow(updated)


@router.delete(
    "/delete-conversation-flow/{conversation_flow_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation_flow(
    conversation_flow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    if not await flows_repo.archive(db, flow_id):
        raise CompatError(404, "conversation flow not found")
    await db.commit()
    _audit(request, "delete-conversation-flow")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v2/list-conversation-flows")
async def list_conversation_flows(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after = None
    if pagination_key:
        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after = ids.decode_conversation_flow_cursor(pagination_key)
    rows = await flows_repo.list_flows(
        db, limit=limit, descending=(sort_order != "ascending"), after=after
    )
    _audit(request, "list-conversation-flows")
    has_more = len(rows) > limit
    page = rows[:limit]
    out: dict[str, Any] = {"items": [serialize_flow(r) for r in page], "has_more": has_more}
    if has_more:
        out["pagination_key"] = ids.encode_conversation_flow_cursor(
            page[-1].created_at, page[-1].id
        )
    return out
