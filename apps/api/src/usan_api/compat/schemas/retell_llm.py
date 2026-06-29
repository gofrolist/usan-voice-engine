"""Pydantic request/response models for the RetellAI-compatible Retell-LLM endpoints (T034).

A Retell-LLM is the "response engine" half of an agent; one ``AgentProfile`` is both the
agent and its response engine (``agent_id`` and ``llm_id`` encode the same row, data-model
§5). Like the agent schemas these are ``extra="allow"`` to capture-and-echo the CRM's full
payload. ``model``/``model_temperature``/``s2s_model`` are accepted but the prompt runs on
the engine's own Vertex pipeline (PHI containment, Constitution II) — the requested model is
echoed, never honored.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class CreateRetellLlmRequest(BaseModel):
    """POST /create-retell-llm. ``general_prompt`` maps to the profile's system prompt;
    ``begin_message`` to its greeting; ``model``* are accepted/echoed only."""

    model_config = ConfigDict(extra="allow")

    start_speaker: str | None = None
    general_prompt: str | None = None
    begin_message: str | None = None
    general_tools: list[Any] | None = None
    states: list[Any] | None = None
    starting_state: str | None = None
    model: str | None = None
    model_temperature: float | None = None
    knowledge_base_ids: list[str] | None = None


class UpdateRetellLlmRequest(BaseModel):
    """PATCH /update-retell-llm — every field optional (partial update)."""

    model_config = ConfigDict(extra="allow")

    general_prompt: str | None = None
    begin_message: str | None = None
    general_tools: list[Any] | None = None
    states: list[Any] | None = None
    starting_state: str | None = None
    model: str | None = None
    model_temperature: float | None = None
    knowledge_base_ids: list[str] | None = None


class LlmResponse(BaseModel):
    """The RetellAI Retell-LLM object. ``extra="allow"`` echoes the CRM's submitted config
    alongside the derived ``llm_id`` / ``version`` / ``is_published``."""

    model_config = ConfigDict(extra="allow")

    llm_id: str
    version: int
    is_published: bool
    last_modification_timestamp: int | None = None
    general_prompt: str | None = None
    begin_message: str | None = None
