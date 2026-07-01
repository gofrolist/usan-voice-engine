"""Call-oriented conversation-flow resolver for the VOICE runtime (Phase 6-runtime-voice).

Resolves a call's bound RUNNABLE flow and computes the node the conversation should be on,
reusing the shared interpreter in flow_runtime. No speak() — voice never generates speech
server-side; the agent's LiveKit LLM speaks under the returned instruction. Never raises for
flow-shape reasons: an unbound / missing / cross-org / non-runnable binding is bound=False.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import flow_runtime
from usan_api.db.base import CallDirection
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import conversation_flows as conversation_flows_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.schemas.runtime import FlowAdvanceResponse, FlowTurn
from usan_api.settings import Settings

_UNBOUND = FlowAdvanceResponse(bound=False)


async def resolve_bound_flow(
    db: AsyncSession, settings: Settings, call_id: uuid.UUID
) -> tuple[dict[str, Any], dict[str, str], str] | None:
    """(flow_config, values, model) for the call's bound RUNNABLE flow, else None.

    Resolves the call -> its winning published agent version (same precedence the
    /agent-config endpoint uses), reads the RAW version.config (AgentConfig(extra="ignore")
    would strip compat_response_engine), decodes the flow binding, loads the org-scoped flow
    (RLS: cross-org id -> None), and returns None unless flow_is_runnable."""
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
    resolved = await agent_profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact_profile_id,
        direction=resolved_direction,
    )
    if resolved is None or resolved.profile_id is None:
        return None
    profile = await agent_profiles_repo.get_profile(db, resolved.profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
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
    merged_custom: dict[str, object] = {}
    flow_defaults = flow_config.get("default_dynamic_variables")
    if isinstance(flow_defaults, dict):
        merged_custom.update(flow_defaults)
    merged_custom.update(call.dynamic_vars or {})
    values = build_vars({}, merged_custom, timezone="", now=datetime.now(UTC))
    model = flow_runtime.flow_model(flow_config, cfg.llm.model)
    return flow_config, values, model


def _assemble_instruction(
    flow_config: dict[str, Any], node: dict[str, Any], values: dict[str, str]
) -> str:
    global_prompt = substitute(str(flow_config.get("global_prompt") or ""), values)
    node_text = substitute(flow_runtime.node_instruction_text(node) or "", values)
    return f"{global_prompt}\n\n{node_text}".strip()


async def advance(
    db: AsyncSession,
    settings: Settings,
    call_id: uuid.UUID,
    current_node_id: str | None,
    turns: Sequence[FlowTurn],
) -> FlowAdvanceResponse:
    """Enter at start when current_node_id is null/stale; else evaluate the current node's
    edges and advance (or remain). Returns bound=False when the call is not bound to a
    runnable flow. FlowTurn's .role/.content match the duck type evaluate_transition consumes."""
    resolved = await resolve_bound_flow(db, settings, call_id)
    if resolved is None:
        return _UNBOUND
    flow_config, values, model = resolved
    if not current_node_id or flow_runtime.node_by_id(flow_config, current_node_id) is None:
        node = flow_runtime.node_by_id(flow_config, flow_config.get("start_node_id"))
    else:
        current = flow_runtime.node_by_id(flow_config, current_node_id)
        assert current is not None  # node_by_id guard above
        dest = await flow_runtime.evaluate_transition(
            current, turns, values, model=model, settings=settings
        )
        node = flow_runtime.node_by_id(flow_config, dest) if dest else current
    if node is None:
        return _UNBOUND  # defensive: start unresolved despite runnable guard
    return FlowAdvanceResponse(
        bound=True,
        node_id=node.get("id"),
        instruction=_assemble_instruction(flow_config, node, values),
        is_end=node.get("type") == "end",
    )
