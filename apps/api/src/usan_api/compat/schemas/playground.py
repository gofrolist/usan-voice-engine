"""Schemas for the RetellAI-compat agent-playground-completion op (Phase 7 slice 1).

A stateless single-turn completion. Advanced request fields (tool_mocks,
current_state, current_node_id, component_id) are accepted for forward-compat but
not acted on this slice; response optional fields default None and are omitted via
model_dump(exclude_none=True). See
docs/superpowers/specs/2026-07-01-retell-parity-phase7-playground-design.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class PlaygroundMessageInput(BaseModel):
    """One input message. `role`/`content` cover the MessageBase variant; the other
    ChatMessageInput oneOf variants (tool-call / transition / injected / sms) are
    tolerated via extra='allow' and skipped when they carry no text `content`."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | None = None
    message_id: str | None = None


class PlaygroundCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[PlaygroundMessageInput]
    dynamic_variables: dict[str, str] | None = None
    tool_mocks: list[Any] | None = None
    current_state: str | None = None
    current_node_id: str | None = None
    component_id: str | None = None

    @field_validator("messages")
    @classmethod
    def _messages_non_empty(cls, v: list[PlaygroundMessageInput]) -> list[PlaygroundMessageInput]:
        if not v:
            raise ValueError("messages must not be empty")
        return v


class PlaygroundMessageOut(BaseModel):
    message_id: str
    role: Literal["agent"] = "agent"
    content: str
    created_timestamp: int


class PlaygroundCompletionResponse(BaseModel):
    messages: list[PlaygroundMessageOut]
    current_state: str | None = None
    current_node_id: str | None = None
    dynamic_variables: dict[str, str] | None = None
    call_ended: bool | None = None
    knowledge_base_retrieved_contents: list[str] | None = None
