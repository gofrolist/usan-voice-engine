"""RetellAI-compatible agent endpoints (feature 003, US3):

  POST   /create-agent                    (201)
  GET    /get-agent/{agent_id}
  GET    /list-agents                     (bare array — single inventory)
  PATCH  /update-agent/{agent_id}
  DELETE /delete-agent/{agent_id}         (204)
  POST   /publish-agent-version/{agent_id}
  GET    /get-agent-versions/{agent_id}

Auth + org-scoped RLS session via ``get_compat_db``. Every op emits a PHI-free audit line
(org id + op + agent id). ``response_model`` is intentionally omitted so FastAPI serializes
the ``extra="allow"`` AgentResponse via ``model_dump`` — preserving the CRM's echoed config.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import agent_bridge, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.agents import (
    CreateAgentRequest,
    ListAgentsRequest,
    PublishAgentVersionRequest,
    UpdateAgentRequest,
)
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-agents"])


def _audit(request: Request, op: str, agent_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, agent_id=agent_id).info("compat agent op={op}")


@router.post("/create-agent", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile, secret = await agent_bridge.bind_agent(db, settings, body)
    response.status_code = status.HTTP_201_CREATED
    _audit(request, "create-agent", ids.encode_agent_id(profile.id))
    return agent_bridge.serialize_agent(profile, webhook_secret=secret).model_dump(
        exclude_none=True
    )


@router.get("/get-agent/{agent_id}")
async def get_agent(
    agent_id: str,
    request: Request,
    # FROZEN (oracle): ?version accepted; current config always served.
    # Pinned by test_get_agent_accepts_version_query_and_serves_current.
    version: int | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    profile = await agent_bridge.get_agent_profile(db, agent_id)
    _audit(request, "get-agent", agent_id)
    return agent_bridge.serialize_agent(profile).model_dump(exclude_none=True)


@router.get("/list-agents")
async def list_agents(
    request: Request,
    pagination_key: str | None = Query(default=None),
    pagination_key_version: int | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    """A BARE array (not wrapped) — the single agent inventory (admin-UI + API agents).
    Keyset cursor over agent_id; the params are accepted for contract compatibility and the
    profile list is bounded by ``limit``."""
    profiles = await agent_bridge.list_agent_profiles(db)
    # Deterministic total order (name, id): list_profiles orders by name; the id tiebreaker
    # makes the keyset exact even across equal names. The cursor must walk THIS order —
    # comparing raw UUIDs (p.id > after) slices an order unrelated to the sort, silently
    # dropping/duplicating rows across pages.
    profiles = sorted(profiles, key=lambda p: (p.name, p.id.bytes))
    if pagination_key:
        # An unparseable cursor yields the unsliced page rather than 422-ing a list.
        with contextlib.suppress(CompatError):
            after = ids.decode_agent_id(pagination_key)
            cut = next((i for i, p in enumerate(profiles) if p.id == after), None)
            # Page strictly AFTER the cursor in (name, id) order; a stale/unknown cursor id
            # (e.g. a since-deleted agent) falls through to the full page — lenient, matching
            # the unparseable-cursor behavior above.
            if cut is not None:
                profiles = profiles[cut + 1 :]
    _audit(request, "list-agents")
    return [agent_bridge.serialize_agent(p).model_dump(exclude_none=True) for p in profiles[:limit]]


@router.patch("/update-agent/{agent_id}")
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    request: Request,
    version: int | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile, secret = await agent_bridge.update_agent(db, settings, agent_id, body)
    _audit(request, "update-agent", agent_id)
    return agent_bridge.serialize_agent(profile, webhook_secret=secret).model_dump(
        exclude_none=True
    )


@router.delete("/delete-agent/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await agent_bridge.delete_agent(db, agent_id)
    _audit(request, "delete-agent", agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/publish-agent-version/{agent_id}")
async def publish_agent_version(
    agent_id: str,
    body: PublishAgentVersionRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    profile = await agent_bridge.publish_agent_version(db, agent_id, body)
    _audit(request, "publish-agent-version", agent_id)
    return agent_bridge.serialize_agent(profile).model_dump(exclude_none=True)


@router.get("/get-agent-versions/{agent_id}")
async def get_agent_versions(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    profile, versions = await agent_bridge.list_agent_versions(db, agent_id)
    _audit(request, "get-agent-versions", agent_id)
    return [
        agent_bridge.serialize_agent_version(profile, v).model_dump(exclude_none=True)
        for v in versions
    ]


@router.delete("/delete-agent-version/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_version(
    agent_id: str,
    request: Request,
    version: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await agent_bridge.delete_agent_version(db, agent_id, version)
    _audit(request, "delete-agent-version", agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/v2/list-agents")
async def list_agents_v2(
    body: ListAgentsRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    """POST /v2/list-agents — paginated list of AgentListItemResponse (oracle shape).

    Returns a paginated wrapper ``{items: [...], pagination_key: null, has_more: false}``.
    This engine is voice-only: a ``channel=chat`` filter always returns an empty list.
    Keyset pagination is not yet implemented; all non-archived agents are returned in a
    single page (has_more=false, pagination_key omitted via exclude_none).
    """
    profiles = await agent_bridge.list_agent_profiles(db)

    # Apply filter_criteria if provided.
    fc = body.filter_criteria
    if fc is not None:
        if fc.channel is not None and fc.channel.value == "chat":
            # Voice-only engine: chat channel always yields an empty list.
            profiles = []
        # query: case-insensitive substring match on agent_name (profile.name).
        if fc.query is not None and profiles:
            q = fc.query.lower()
            profiles = [p for p in profiles if q in (p.name or "").lower()]

    items = [
        agent_bridge.serialize_agent_list_item(p).model_dump(exclude_none=True) for p in profiles
    ]
    _audit(request, "list-agents", "")
    # Return paginated wrapper. pagination_key=None is excluded via exclude_none=True
    # (oracle: PaginatedResponseBase fields are optional). has_more is always False
    # (single-page implementation).
    return {"items": items, "has_more": False}
