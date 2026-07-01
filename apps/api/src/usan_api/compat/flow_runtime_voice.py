"""Call-oriented conversation-flow resolver for the VOICE runtime (Phase 6-runtime-voice).

Resolves a call's bound RUNNABLE flow and computes the node the conversation should be on,
reusing the shared interpreter in flow_runtime. No speak() — voice never generates speech
server-side; the agent's LiveKit LLM speaks under the returned instruction. Never raises for
flow-shape reasons: an unbound / missing / cross-org / non-runnable binding is bound=False.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import flow_runtime
from usan_api.db.base import CallDirection
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import conversation_flows as conversation_flows_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.schemas.runtime import FlowAdvanceResponse, FlowTurn
from usan_api.settings import Settings


async def resolve_bound_flow(
    db: AsyncSession, settings: Settings, call_id: uuid.UUID
) -> tuple[dict[str, Any], dict[str, str], str, uuid.UUID] | None:
    """(flow_config, values, model, flow_uuid) for the call's bound RUNNABLE flow, else None.

    Resolves the call -> its winning published agent version (same precedence the
    /agent-config endpoint uses, via the single-load agent_profiles_repo.resolve_published_version
    helper), reads the RAW version.config (AgentConfig(extra="ignore") would strip
    compat_response_engine), decodes the flow binding, loads the org-scoped flow (RLS: cross-org
    id -> None), and returns None unless flow_is_runnable. flow_uuid is returned so ``advance``
    can qualify the cursor to this flow (repoint-safety)."""
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        return None
    contact_profile_id: uuid.UUID | None = None
    if call.contact_id is not None:
        contact = await contacts_repo.get_contact(db, call.contact_id)
        if contact is not None:
            contact_profile_id = contact.agent_profile_id
    resolved_direction: Literal["inbound", "outbound"] = (
        "outbound" if call.direction is CallDirection.OUTBOUND else "inbound"
    )
    version = await agent_profiles_repo.resolve_published_version(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact_profile_id,
        direction=resolved_direction,
    )
    if version is None:
        return None
    raw = version.config or {}

    flow_uuid = flow_runtime.bound_flow_id(raw)
    if flow_uuid is None:
        return None
    flow_row = await conversation_flows_repo.get(db, flow_uuid)
    if flow_row is None:
        return None
    flow_config = flow_row.config or {}
    if not flow_runtime.flow_is_runnable(flow_config):
        return None

    cfg = AgentConfig.model_validate(raw)
    # Merge flow defaults under the call's stored dynamic_vars (the call's personalization),
    # mirroring the chat flow path. Mid-call agent-side var updates are NOT reflected (deferred).
    values = flow_runtime.merge_flow_values(flow_config, call.dynamic_vars or {})
    model = flow_runtime.flow_model(flow_config, cfg.llm.model)
    return flow_config, values, model, flow_uuid


async def advance(
    db: AsyncSession,
    settings: Settings,
    call_id: uuid.UUID,
    cursor: str | None,
    turns: Sequence[FlowTurn],
) -> FlowAdvanceResponse:
    """Enter at start when the (flow-qualified) cursor is null/stale/foreign; else evaluate the
    current node's edges and advance (or remain). Returns bound=False when the call is not bound
    to a runnable flow. FlowTurn's .role/.content match the duck type evaluate_transition
    consumes."""
    resolved = await resolve_bound_flow(db, settings, call_id)
    if resolved is None:
        return FlowAdvanceResponse(bound=False)
    flow_config, values, model, flow_uuid = resolved
    node_id = flow_runtime.cursor_for_flow(cursor, flow_uuid)
    current = flow_runtime.node_by_id(flow_config, node_id)
    if current is None:
        node = flow_runtime.node_by_id(flow_config, flow_config.get("start_node_id"))
    else:
        dest = await flow_runtime.evaluate_transition(
            current, turns, values, model=model, settings=settings
        )
        node = flow_runtime.node_by_id(flow_config, dest) if dest else current
    if node is None:
        # defensive: start unresolved despite the runnable guard
        return FlowAdvanceResponse(bound=False)
    return FlowAdvanceResponse(
        bound=True,
        node_id=node.get("id"),
        cursor=flow_runtime.make_cursor(flow_uuid, node.get("id")),
        instruction=flow_runtime.assemble_instruction(flow_config, node, values),
        is_end=node.get("type") == "end",
    )
