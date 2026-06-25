"""Pydantic request/response models for the RetellAI-compatible call endpoints (feature 003).

The response **Call object** (``CompatCall``) is the central RetellAI resource, assembled by
``compat.call_serializer``. Field names + shapes are the external contract, frozen and
validated against the captured CRM oracle (tasks.md contract-freeze gate).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# --- Enums ------------------------------------------------------------------------------
class DataStorageSetting(StrEnum):
    everything = "everything"
    everything_except_pii = "everything_except_pii"
    basic_attributes_only = "basic_attributes_only"


# --- Requests ---------------------------------------------------------------------------
class CreatePhoneCallRequest(BaseModel):
    """POST /v2/create-phone-call. RetellAI has no Contact concept — ``to_number`` drives a
    lazy Contact upsert (data-model §6)."""

    from_number: str
    to_number: str
    override_agent_id: str | None = None
    # FROZEN (oracle AgentVersionReference): int version OR string tag ("latest"/"prod").
    # Numeric selects that version; a string tag serves the current published version (MVP).
    override_agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    # FROZEN (oracle additionalProperties:string): accept + echo; values coerced to str.
    custom_sip_headers: dict[str, str] | None = None

    @field_validator("custom_sip_headers", mode="before")
    @classmethod
    def _coerce_sip_header_values_to_str(cls, v: Any) -> dict[str, str] | None:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("custom_sip_headers must be a mapping")
        return {k: str(val) for k, val in v.items()}


class UpdateCallRequest(BaseModel):
    """PATCH /v2/update-call/{id}. ``override_dynamic_variables`` is the oracle field name on
    THIS op; ``data_storage_setting`` is enum-validated (no-op behavior); ``custom_attributes``
    is accepted/echoed."""

    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    override_dynamic_variables: dict[str, Any] | None = None
    data_storage_setting: DataStorageSetting | None = None
    custom_attributes: dict[str, Any] | None = None


class ListCallsRequest(BaseModel):
    """POST /v3/list-calls — filterable, cursor- (or skip-) paginated."""

    filter_criteria: dict[str, Any] | None = None
    sort_order: str = "descending"
    limit: int = Field(default=50, ge=1, le=1000)
    # Opaque keyset cursor (the last returned call_id of the previous page); XOR ``skip``.
    pagination_key: str | None = None
    skip: int | None = Field(default=None, ge=0)
    include_total: bool = False

    @model_validator(mode="after")
    def _skip_xor_pagination_key(self) -> ListCallsRequest:
        # The two paginators are mutually exclusive: combining them applies the keyset WHERE
        # *and* an OFFSET on top, silently double-paginating (a coverage gap). Reject up front
        # with a clean 422 rather than returning a quietly wrong page.
        if self.skip is not None and self.pagination_key is not None:
            raise ValueError("skip and pagination_key are mutually exclusive")
        return self


# --- Response sub-objects ---------------------------------------------------------------
class TranscriptUtterance(BaseModel):
    # FROZEN: role enum agent|user|transfer_target; words [] (oracle Utterance).
    role: str
    content: str
    # Word-level timing is not captured natively; emitted empty (RetellAI marks it optional).
    words: list[Any] = Field(default_factory=list)


class CallCost(BaseModel):
    combined_cost: float
    total_duration_seconds: int | None = None
    product_costs: list[dict[str, Any]] = Field(default_factory=list)
    pricing_version: str | None = None


class CallAnalysis(BaseModel):
    call_summary: str | None = None
    in_voicemail: bool = False
    # FROZEN: null now; non-null vocab is title-case Negative/Positive/Neutral/Unknown
    # (oracle CallAnalysis).
    user_sentiment: str | None = None
    call_successful: bool | None = None
    custom_analysis_data: dict[str, Any] | None = None


# --- The Call object (shared by get-call / list-calls / webhooks) -----------------------
class CompatCall(BaseModel):
    call_id: str
    call_type: str = "phone_call"
    agent_id: str | None = None
    agent_name: str | None = None
    agent_version: int | None = None
    call_status: str
    from_number: str | None = None
    to_number: str | None = None
    direction: str
    telephony_identifier: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    retell_llm_dynamic_variables: dict[str, Any] = Field(default_factory=dict)
    # FROZEN: null pre-end; shape per oracle V3CallBase / CallLatency.
    collected_dynamic_variables: dict[str, Any] | None = None
    start_timestamp: int | None = None
    end_timestamp: int | None = None
    duration_ms: int | None = None
    transcript: str | None = None
    transcript_object: list[TranscriptUtterance] = Field(default_factory=list)
    recording_url: str | None = None
    public_log_url: str | None = None  # no native source -> null
    disconnection_reason: str | None = None
    call_analysis: CallAnalysis | None = None
    call_cost: CallCost | None = None
    # FROZEN: null pre-end; shape per oracle V3CallBase / CallLatency.
    latency: dict[str, Any] | None = None
    llm_token_usage: dict[str, Any] | None = None


class ListCallsResponse(BaseModel):
    """POST /v3/list-calls envelope: { items, pagination_key, has_more, total? }."""

    items: list[CompatCall] = Field(default_factory=list)
    pagination_key: str | None = None
    has_more: bool = False
    total: int | None = None
