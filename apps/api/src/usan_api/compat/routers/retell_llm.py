"""RetellAI-compatible Retell-LLM (response engine) endpoints (feature 003, US3):

  POST   /create-retell-llm        (201)
  GET    /get-retell-llm/{llm_id}
  PATCH  /update-retell-llm/{llm_id}
  DELETE /delete-retell-llm/{llm_id}   (204)
  GET    /list-retell-llms             (bare array)

A Retell-LLM is the response-engine view of an ``AgentProfile``; ``llm_id`` and ``agent_id``
encode the same row (data-model §5). ``response_model`` is omitted so the ``extra="allow"``
LlmResponse echo survives serialization. PENDING-FREEZE (oracle): the exact list prefix
(root vs ``/v2/list-retell-llms``) is pinned against the captured CRM usage.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import agent_bridge, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest, UpdateRetellLlmRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-retell-llm"])


def _audit(request: Request, op: str, llm_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, llm_id=llm_id).info("compat retell-llm op={op}")


@router.post("/create-retell-llm", status_code=status.HTTP_201_CREATED)
async def create_retell_llm(
    body: CreateRetellLlmRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await agent_bridge.create_response_engine(db, settings, body)
    response.status_code = status.HTTP_201_CREATED
    _audit(request, "create-retell-llm", ids.encode_llm_id(profile.id))
    return agent_bridge.serialize_llm(profile).model_dump()


@router.get("/get-retell-llm/{llm_id}")
async def get_retell_llm(
    llm_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    profile = await agent_bridge.get_llm_profile(db, llm_id)
    _audit(request, "get-retell-llm", llm_id)
    return agent_bridge.serialize_llm(profile).model_dump()


@router.patch("/update-retell-llm/{llm_id}")
async def update_retell_llm(
    llm_id: str,
    body: UpdateRetellLlmRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await agent_bridge.update_response_engine(db, settings, llm_id, body)
    _audit(request, "update-retell-llm", llm_id)
    return agent_bridge.serialize_llm(profile).model_dump()


@router.delete("/delete-retell-llm/{llm_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_retell_llm(
    llm_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    # The response engine and the agent are the same profile; deleting either archives it.
    profile = await agent_bridge.get_llm_profile(db, llm_id)
    await agent_bridge.delete_agent(db, ids.encode_agent_id(profile.id))
    _audit(request, "delete-retell-llm", llm_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/list-retell-llms")
async def list_retell_llms(
    request: Request,
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    profiles = await agent_bridge.list_agent_profiles(db)
    _audit(request, "list-retell-llms")
    return [agent_bridge.serialize_llm(p).model_dump() for p in profiles[:limit]]
