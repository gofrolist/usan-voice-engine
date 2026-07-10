import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from loguru import logger
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

# Upper bound on one flow-advance (up to two sequential Vertex calls). A hang or error must
# not 500 a live call — the agent falls back to the single-prompt path on bound=False.
_FLOW_ADVANCE_TIMEOUT_S = 20.0


def _project_config_for_worker(
    resolved: ResolvedAgentConfig, *, external_tools_enabled: bool
) -> dict[str, Any]:
    """Project the resolved config to the WORKER-facing shape (Surface 3 security seam).

    ``tools.external_tools`` is reduced to the LLM-facing fields plus the ``terminates_call``
    behavior flag — ``{name, description, parameters, terminates_call}`` — stripping each tool's
    ``url``/``method``/``timeout_s``. The client edge-function URL and the caller secret therefore
    never leave ``apps/api``; the worker is structurally unable to learn a tool's endpoint
    (design §5). ``terminates_call`` is a behavior flag (not a secret), so the worker needs it to
    hang up after the client's end_call.
    When the feature flag is off, the list is emptied (the ingest also does not persist any,
    so this is belt-and-suspenders). Returns a JSON-safe dict (uuids stringified)."""
    payload: dict[str, Any] = resolved.model_dump(mode="json")
    tools = payload.get("config", {}).get("tools") or {}
    external = tools.get("external_tools") or []
    if external_tools_enabled:
        tools["external_tools"] = [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
                # Behavior flag (not a secret): the worker must know to hang up after this tool.
                "terminates_call": t.get("terminates_call", False),
            }
            for t in external
        ]
    else:
        tools["external_tools"] = []
    return payload


@router.get("/agent-config")
async def get_agent_config(
    direction: Literal["inbound", "outbound"],
    call_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> dict[str, Any]:
    """Resolve the published agent config for a call (or a direction default).

    Worker-token scope: the resolved config is profile-global, not per-contact PHI. A
    missing/unknown call_id is not an error — resolution falls back to the direction
    default and ultimately DEFAULT_AGENT_CONFIG. Always 200 with a usable config.

    The response is the ``ResolvedAgentConfig`` shape with ``tools.external_tools`` projected
    to LLM-facing fields only (``_project_config_for_worker``); ``response_model`` is dropped
    because that projected shape is intentionally NOT a full API-side ``ExternalToolSpec``
    (no ``url``) — re-validating it would fail on the required ``url`` field.
    """
    override_id: uuid.UUID | None = None
    contact_profile_id: uuid.UUID | None = None
    resolved_direction: Literal["inbound", "outbound"] = direction
    if call_id is not None:
        # Outbound always passes a call_id. Inbound normally fetches config with call_id=None
        # (before the contact lookup) and resolves to the per-direction default — EXCEPT when the
        # Surface 2A inbound-call-router set a profile_override on the call: the worker then
        # re-fetches by call_id and this branch resolves that override (direction stays inbound).
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
        resolved = ResolvedAgentConfig(
            source="default", profile_id=None, version=None, config=DEFAULT_AGENT_CONFIG
        )
    return _project_config_for_worker(
        resolved, external_tools_enabled=settings.compat_external_tools_enabled
    )


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
    try:
        return await asyncio.wait_for(
            flow_runtime_voice.advance(db, settings, body.call_id, body.cursor, body.turns),
            timeout=_FLOW_ADVANCE_TIMEOUT_S,
        )
    except Exception as exc:
        # advance() is read-only (resolve + Vertex classify; the cursor lives agent-side), so a
        # timeout/error leaves no partial DB state. Fall back to the single-prompt path rather
        # than 500 the live call. Type name only — a Vertex error can embed prompt text.
        logger.bind(call_id=str(body.call_id), err=type(exc).__name__).warning(
            "flow-advance failed; falling back to single-prompt"
        )
        return FlowAdvanceResponse(bound=False)
