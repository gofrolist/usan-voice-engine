"""Pydantic models for the RetellAI-compatible chat-agent endpoints (Phase 4c-1).

A chat agent overlays an AgentProfile (channel='chat'); the CRM's submitted ChatAgentRequest
config is echoed verbatim via compat_extras['chat_agent']. Every model is ``extra='allow'`` so a
migrating CRM is never rejected for a chat-config field the engine persists-not-honors.
``response_engine`` is required on create, optional on update (PATCH semantics).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat.schemas.agents import ResponseEngine


class ChatAgentCreateRequest(BaseModel):
    """POST /create-chat-agent. ``response_engine.llm_id`` binds the chat agent onto the profile
    a prior ``create-retell-llm`` made (only ``type='retell-llm'`` is honored)."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine
    agent_name: str | None = None


class ChatAgentUpdateRequest(BaseModel):
    """PATCH /update-chat-agent — every field optional (partial update)."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine | None = None
    agent_name: str | None = None


class ChatAgentResponse(BaseModel):
    """The RetellAI chat-agent object. ``extra='allow'`` echoes the CRM's submitted config
    (held in compat_extras['chat_agent']) alongside the engine-derived fields. Net oracle-required
    fields: agent_id, response_engine, last_modification_timestamp."""

    model_config = ConfigDict(extra="allow")

    agent_id: str
    response_engine: dict[str, Any]
    version: int
    is_published: bool
    last_modification_timestamp: int | None = None
    agent_name: str | None = None
