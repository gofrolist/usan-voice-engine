"""RetellAI-compat agent-playground-completion router (Phase 7 slice 1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import playground_service
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.playground import PlaygroundCompletionRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-playground"])


@router.post("/agent-playground-completion/{agent_id}")
async def agent_playground_completion(
    agent_id: str,
    body: PlaygroundCompletionRequest,
    request: Request,
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = await playground_service.run_playground_completion(
        db, settings, agent_id=agent_id, version=version, request=body
    )
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op="agent-playground-completion").info("compat playground op")
    return result.model_dump(exclude_none=True)
