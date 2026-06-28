"""RetellAI-compatible chat-agent endpoints (Phase 4c-1):

  POST   /create-chat-agent                       (201)
  GET    /get-chat-agent/{agent_id}
  GET    /get-chat-agent-versions/{agent_id}
  GET    /list-chat-agents                        (bare array, deprecated)
  PATCH  /update-chat-agent/{agent_id}
  DELETE /delete-chat-agent/{agent_id}            (204)
  POST   /publish-chat-agent/{agent_id}           (200, no body, deprecated)

A chat agent overlays an AgentProfile (channel='chat'). ``response_model`` is omitted so the
``extra='allow'`` ChatAgentResponse echo survives serialization (model_dump(exclude_none=True)).
Auth + org-scoped RLS via get_compat_db; every op emits a PHI-free audit line.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import chat_agent_bridge, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import ChatAgentCreateRequest, ChatAgentUpdateRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-chat-agents"])


def _audit(request: Request, op: str, agent_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, agent_id=agent_id).info("compat chat-agent op={op}")


@router.post("/create-chat-agent", status_code=status.HTTP_201_CREATED)
async def create_chat_agent(
    body: ChatAgentCreateRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.create_chat_agent(db, settings, body)
    response.status_code = status.HTTP_201_CREATED
    _audit(request, "create-chat-agent", ids.encode_agent_id(profile.id))
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.get("/get-chat-agent/{agent_id}")
async def get_chat_agent(
    agent_id: str,
    request: Request,
    # ?version accepted (AgentVersionReference); the current published view is always served.
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.get_chat_agent(db, agent_id)
    _audit(request, "get-chat-agent", agent_id)
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.get("/get-chat-agent-versions/{agent_id}")
async def get_chat_agent_versions(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    profile, versions = await chat_agent_bridge.list_chat_agent_versions(db, agent_id)
    _audit(request, "get-chat-agent-versions", agent_id)
    return [
        chat_agent_bridge.serialize_chat_agent_version(profile, v).model_dump(exclude_none=True)
        for v in versions
    ]


@router.get("/list-chat-agents")
async def list_chat_agents(
    request: Request,
    pagination_key: str | None = Query(default=None),
    pagination_key_version: int | None = Query(default=None),
    is_latest: bool | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    """Bare array (deprecated). Keyset cursor over (name, id); ``is_latest`` /
    ``pagination_key_version`` are accepted for contract compatibility and the current published
    view is returned per profile."""
    profiles = await chat_agent_bridge.list_chat_agents(db)
    profiles = sorted(profiles, key=lambda p: (p.name, p.id.bytes))
    if pagination_key:
        with contextlib.suppress(CompatError):
            after = ids.decode_agent_id(pagination_key)
            cut = next((i for i, p in enumerate(profiles) if p.id == after), None)
            if cut is not None:
                profiles = profiles[cut + 1 :]
    _audit(request, "list-chat-agents")
    return [
        chat_agent_bridge.serialize_chat_agent(p).model_dump(exclude_none=True)
        for p in profiles[:limit]
    ]


@router.patch("/update-chat-agent/{agent_id}")
async def update_chat_agent(
    agent_id: str,
    body: ChatAgentUpdateRequest,
    request: Request,
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.update_chat_agent(db, settings, agent_id, body)
    _audit(request, "update-chat-agent", agent_id)
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.delete("/delete-chat-agent/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_agent_bridge.delete_chat_agent(db, agent_id)
    _audit(request, "delete-chat-agent", agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/publish-chat-agent/{agent_id}")
async def publish_chat_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    """Deprecated thin publish — the oracle returns 200 with no response body."""
    await chat_agent_bridge.publish_chat_agent(db, agent_id)
    _audit(request, "publish-chat-agent", agent_id)
    return Response(status_code=status.HTTP_200_OK)
