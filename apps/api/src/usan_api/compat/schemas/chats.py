"""Pydantic request/response models for the RetellAI-compatible chat endpoints (phase 4a).

Field names and shapes mirror the RetellAI V3 Chat surface.  The response model
``CompatChat`` deliberately uses ``| None = None`` (not ``Field(default_factory=…)``) so
that ``response_model_exclude_none=True`` at the route omits empty fields — required to
satisfy the oracle V3ChatResponse ``not``-clause that forbids ``transcript`` /
``message_with_tool_calls`` on list items.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateChatRequest(BaseModel):
    """POST /create-chat. Oracle: agent_id required; the rest optional."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None


class CreateSmsChatRequest(BaseModel):
    """POST /create-sms-chat. Oracle: from_number + to_number required; the rest optional."""

    model_config = ConfigDict(extra="forbid")

    from_number: str = Field(min_length=1)
    to_number: str = Field(min_length=1)
    override_agent_id: str | None = Field(default=None, min_length=1)
    override_agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None


class CreateChatCompletionRequest(BaseModel):
    """POST /create-chat-completion. chat_id + content required; no vars/metadata."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    content: str


class UpdateChatRequest(BaseModel):
    """PATCH /update-chat. All optional. data_storage_setting is accepted-and-ignored (4a)."""

    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] | None = None
    data_storage_setting: Literal["everything", "basic_attributes_only"] | None = None
    override_dynamic_variables: dict[str, str] | None = None
    custom_attributes: dict[str, Any] | None = None


class ListChatsRequest(BaseModel):
    """POST /v3/list-chats — filterable, cursor- (or skip-) paginated. Mirrors ListCallsRequest."""

    model_config = ConfigDict(extra="forbid")

    filter_criteria: dict[str, Any] | None = None
    sort_order: str = "descending"
    limit: int = Field(default=50, ge=1, le=1000)
    pagination_key: str | None = None
    skip: int | None = Field(default=None, ge=0)
    include_total: bool = False

    @model_validator(mode="after")
    def _skip_xor_pagination_key(self) -> ListChatsRequest:
        if self.skip is not None and self.pagination_key is not None:
            raise ValueError("skip and pagination_key are mutually exclusive")
        return self


class CompatChatMessage(BaseModel):
    role: str
    content: str
    message_id: str
    created_timestamp: int


class CompatChat(BaseModel):
    chat_id: str
    agent_id: str
    chat_status: str
    version: int | None = None
    chat_type: str = "api_chat"
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    start_timestamp: int | None = None
    end_timestamp: int | None = None
    transcript: str | None = None
    message_with_tool_calls: list[CompatChatMessage] | None = None


class CompatChatCompletion(BaseModel):
    messages: list[CompatChatMessage] = Field(default_factory=list)


class ListChatsResponse(BaseModel):
    items: list[CompatChat] = Field(default_factory=list)
    pagination_key: str | None = None
    has_more: bool = False
    total: int | None = None
