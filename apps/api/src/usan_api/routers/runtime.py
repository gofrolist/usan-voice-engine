import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_worker_token
from usan_api.db.base import CallDirection
from usan_api.db.session import get_db
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, ResolvedAgentConfig

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


@router.get("/agent-config", response_model=ResolvedAgentConfig)
async def get_agent_config(
    direction: Literal["inbound", "outbound"],
    call_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> ResolvedAgentConfig:
    """Resolve the published agent config for a call (or a direction default).

    Worker-token scope: the resolved config is profile-global, not per-elder PHI. A
    missing/unknown call_id is not an error — resolution falls back to the direction
    default and ultimately DEFAULT_AGENT_CONFIG. Always 200 with a usable config.
    """
    override_id: uuid.UUID | None = None
    elder_profile_id: uuid.UUID | None = None
    resolved_direction: Literal["inbound", "outbound"] = direction
    if call_id is not None:
        # This branch only fires for outbound: the agent fetches inbound config with
        # call_id=None (before the elder lookup), so an inbound call never reaches here
        # and inbound resolves to the per-direction default by design.
        call = await calls_repo.get_call(db, call_id)
        if call is not None:
            override_id = call.profile_override
            resolved_direction = (
                "outbound" if call.direction is CallDirection.OUTBOUND else "inbound"
            )
            if call.elder_id is not None:
                elder = await elders_repo.get_elder(db, call.elder_id)
                if elder is not None:
                    elder_profile_id = elder.agent_profile_id
    resolved = await agent_profiles_repo.resolve_agent_config(
        db,
        profile_override=override_id,
        elder_profile_id=elder_profile_id,
        direction=resolved_direction,
    )
    if resolved is None:
        return ResolvedAgentConfig(
            source="default", profile_id=None, version=None, config=DEFAULT_AGENT_CONFIG
        )
    return resolved
