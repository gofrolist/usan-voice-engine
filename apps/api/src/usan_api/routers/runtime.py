import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_worker_token
from usan_api.compat import flow_runtime_voice
from usan_api.db.base import CallDirection
from usan_api.db.session import get_db
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, ResolvedAgentConfig
from usan_api.schemas.runtime import FlowAdvanceRequest, FlowAdvanceResponse
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


@router.get("/agent-config", response_model=ResolvedAgentConfig)
async def get_agent_config(
    direction: Literal["inbound", "outbound"],
    call_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> ResolvedAgentConfig:
    """Resolve the published agent config for a call (or a direction default).

    Worker-token scope: the resolved config is profile-global, not per-contact PHI. A
    missing/unknown call_id is not an error — resolution falls back to the direction
    default and ultimately DEFAULT_AGENT_CONFIG. Always 200 with a usable config.
    """
    override_id: uuid.UUID | None = None
    contact_profile_id: uuid.UUID | None = None
    resolved_direction: Literal["inbound", "outbound"] = direction
    if call_id is not None:
        # This branch only fires for outbound: the agent fetches inbound config with
        # call_id=None (before the contact lookup), so an inbound call never reaches here
        # and inbound resolves to the per-direction default by design.
        call = await calls_repo.get_call(db, call_id)
        if call is not None:
            override_id = call.profile_override
            resolved_direction = (
                "outbound" if call.direction is CallDirection.OUTBOUND else "inbound"
            )
            if call.contact_id is not None:
                contact = await contacts_repo.get_contact(db, call.contact_id)
                if contact is not None:
                    contact_profile_id = contact.agent_profile_id
    resolved = await agent_profiles_repo.resolve_agent_config(
        db,
        profile_override=override_id,
        contact_profile_id=contact_profile_id,
        direction=resolved_direction,
    )
    if resolved is None:
        return ResolvedAgentConfig(
            source="default", profile_id=None, version=None, config=DEFAULT_AGENT_CONFIG
        )
    return resolved


@router.post("/flow-advance", response_model=FlowAdvanceResponse)
async def flow_advance(
    body: FlowAdvanceRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> FlowAdvanceResponse:
    """Advance a call's bound conversation flow one node given the recent turns. Worker-token
    scoped; flag-gated. Returns bound=False when the flag is off or the call is not bound to a
    runnable flow (the agent then takes the single-prompt path). Raises only if Vertex raises."""
    if not settings.flow_runtime_voice_enabled:
        return FlowAdvanceResponse(bound=False)
    return await flow_runtime_voice.advance(db, settings, body.call_id, body.cursor, body.turns)
