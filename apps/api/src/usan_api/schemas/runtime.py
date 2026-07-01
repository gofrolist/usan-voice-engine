"""Runtime worker-facing schemas (Phase 6-runtime-voice)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class FlowTurn(BaseModel):
    # role is already mapped by the agent: "agent" for the assistant, "user" otherwise.
    # Attribute access (.role/.content) matches the duck type flow_runtime.evaluate_transition
    # consumes, so a list[FlowTurn] is passed straight through as conversation history.
    role: str
    content: str


class FlowAdvanceRequest(BaseModel):
    call_id: uuid.UUID
    current_node_id: str | None = None
    turns: list[FlowTurn] = Field(default_factory=list)


class FlowAdvanceResponse(BaseModel):
    bound: bool
    node_id: str | None = None
    instruction: str | None = None
    is_end: bool = False
