"""Pydantic request/response models for the RetellAI-compatible agent endpoints (T033).

RetellAI agents carry 100+ optional config fields the engine does not natively model. Each
request/response model is therefore ``extra="allow"``: unrecognized inbound fields are
captured (and echoed back on the response via ``compat_extras``), so a migrating CRM is
never rejected for sending a field this engine ignores (FR-030). The handful of fields the
engine actually bridges to an ``AgentProfile`` are typed explicitly. All shapes have been
validated against the captured CRM oracle; the contract is frozen.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Events a webhook subscription may select (mirrors the compat webhook table CHECK).
WEBHOOK_EVENTS = ("call_started", "call_ended", "call_analyzed")


class ResponseEngine(BaseModel):
    """``response_engine`` on an agent — the Retell-LLM (or other engine) it speaks through.
    ``llm_id`` decodes to the SAME AgentProfile as the agent (data-model §5)."""

    model_config = ConfigDict(extra="allow")

    type: str = "retell-llm"
    llm_id: str | None = None
    version: int | None = None


class CreateAgentRequest(BaseModel):
    """POST /create-agent. ``response_engine.llm_id`` binds the agent half onto the profile a
    prior ``create-retell-llm`` made."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine
    voice_id: str = Field(min_length=1)
    agent_name: str | None = None
    language: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] | None = None
    version_title: str | None = None


class UpdateAgentRequest(BaseModel):
    """PATCH /update-agent — every field optional (partial update / PATCH semantics)."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine | None = None
    voice_id: str | None = Field(default=None, min_length=1)
    agent_name: str | None = None
    language: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] | None = None
    version_title: str | None = None


class PublishAgentVersionRequest(BaseModel):
    """POST /publish-agent-version/{agent_id} (FR-032). The server assigns the next version
    number and returns it in the response; the client-supplied ``version`` field is advisory
    and the published response echoes the server-assigned version (contract frozen)."""

    model_config = ConfigDict(extra="allow")

    version: int = Field(ge=0)
    version_title: str | None = None
    version_description: str | None = None


class AgentResponse(BaseModel):
    """The RetellAI agent object returned by create/get/list/update/publish. ``extra="allow"``
    so the CRM's submitted config (held in ``compat_extras``) is echoed verbatim alongside the
    engine-authoritative derived fields (agent_id, version, is_published, …)."""

    model_config = ConfigDict(extra="allow")

    agent_id: str
    agent_name: str | None = None
    response_engine: dict[str, Any]
    voice_id: str | None = None
    version: int
    is_published: bool
    last_modification_timestamp: int | None = None
    language: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] | None = None


# ---------------------------------------------------------------------------
# POST /v2/list-agents schemas (Task 7 — oracle shape, paginated wrapper)
# ---------------------------------------------------------------------------


class ChannelStringFilter(BaseModel):
    """StringFilter specialised for the ``channel`` field (op: eq, value: voice|chat)."""

    model_config = ConfigDict(extra="allow")

    type: Literal["string"] = "string"
    op: Literal["eq"] = "eq"
    value: Literal["voice", "chat"]


class AgentListFilter(BaseModel):
    """Filter criteria for POST /v2/list-agents (oracle: AgentListFilter ~1332-1350)."""

    model_config = ConfigDict(extra="allow")

    channel: ChannelStringFilter | None = None
    query: str | None = None


class ListAgentsRequest(BaseModel):
    """Request body for POST /v2/list-agents."""

    model_config = ConfigDict(extra="allow")

    filter_criteria: AgentListFilter | None = None
    pagination_key: str | None = None
    limit: int | None = None


class AgentListItemResponse(BaseModel):
    """Smaller per-item shape for the paginated list (oracle: AgentListItemResponse ~1353-1387).

    ``tags`` is an object (str → AgentRootTagState) in the oracle; we return an empty dict by
    default (no native tag concept) and ``extra="allow"`` keeps us forward-compatible.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    agent_name: str
    channel: str
    user_modified_timestamp: int
    tags: dict[str, Any] = Field(default_factory=dict)
