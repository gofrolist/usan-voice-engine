import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ToolCallRequest(BaseModel):
    """Base for in-call tool requests: the call this tool action belongs to.

    The handler asserts this matches the JWT `call_id` claim and derives elder_id
    from the call — elder_id is never accepted from the request.
    """

    call_id: uuid.UUID


class LogWellnessRequest(ToolCallRequest):
    mood: int | None = Field(default=None, ge=1, le=5)
    pain_level: int | None = Field(default=None, ge=0, le=10)
    notes: str | None = Field(default=None, max_length=2000)


class LoggedResponse(BaseModel):
    id: int


class FlagForFollowupRequest(ToolCallRequest):
    severity: Literal["routine", "urgent"]
    category: Literal["medical", "emotional", "medication", "safety", "other"]
    reason: str = Field(min_length=1, max_length=2000)


class FollowupFlaggedResponse(BaseModel):
    id: int


class LogMedicationRequest(ToolCallRequest):
    medication_name: str = Field(min_length=1, max_length=200)
    taken: bool
    reported_time: datetime | None = None


class MedicationScheduleItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dosage: str | None = Field(default=None, max_length=200)
    times: list[str] = Field(default_factory=list)


class GetTodayMedsRequest(ToolCallRequest):
    pass


class TodayMedsResponse(BaseModel):
    medications: list[MedicationScheduleItem]


class EndCallRequest(ToolCallRequest):
    reason: str = Field(min_length=1, max_length=100)


class CallEndedResponse(BaseModel):
    status: str


class ScheduleCallbackRequest(ToolCallRequest):
    requested_time_text: str = Field(min_length=1, max_length=200)
    requested_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("requested_at")
    @classmethod
    def _assume_utc(cls, v: datetime | None) -> datetime | None:
        # A naive ISO string from the LLM (no offset/Z) would land in the TIMESTAMPTZ
        # column under an implicit session tz. Treat a tz-naive value as UTC; offset/Z
        # values are already tz-aware and pass through unchanged.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class CallbackScheduledResponse(BaseModel):
    id: int


class TranscriptSegmentIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1)
    tool_name: str | None = Field(default=None, max_length=200)
    tool_args: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime | None = None


class LogTranscriptRequest(ToolCallRequest):
    segments: list[TranscriptSegmentIn] = Field(min_length=1, max_length=500)


class TranscriptLoggedResponse(BaseModel):
    count: int


class TurnMetricIn(BaseModel):
    turn_index: int = Field(ge=0)
    eou_delay_ms: int | None = Field(default=None, ge=0)
    transcription_delay_ms: int | None = Field(default=None, ge=0)
    stt_duration_ms: int | None = Field(default=None, ge=0)
    llm_ttft_ms: int | None = Field(default=None, ge=0)
    tts_ttfb_ms: int | None = Field(default=None, ge=0)
    llm_completion_tokens: int | None = Field(default=None, ge=0)
    tts_characters: int | None = Field(default=None, ge=0)


class MetricsUsageIn(BaseModel):
    llm_prompt_tokens: int = Field(default=0, ge=0)
    llm_completion_tokens: int = Field(default=0, ge=0)
    tts_characters: int = Field(default=0, ge=0)
    stt_audio_seconds: float = Field(default=0.0, ge=0.0)
    session_duration_seconds: float | None = Field(default=None, ge=0.0)


class LogMetricsRequest(BaseModel):
    call_id: uuid.UUID
    turns: list[TurnMetricIn] = Field(default_factory=list, max_length=500)
    usage: MetricsUsageIn = Field(default_factory=MetricsUsageIn)


class MetricsAcceptedResponse(BaseModel):
    call_id: uuid.UUID
    cost_total_usd: Decimal


class SendSmsRequest(ToolCallRequest):
    # The LLM selects a template KEY only; it never authors free text (design §6.1).
    template_key: str = Field(min_length=1, max_length=64)


class SmsQueuedResponse(BaseModel):
    id: uuid.UUID
    status: str


class SendInfoSmsRequest(ToolCallRequest):
    # No fields beyond call_id: the body is a fixed, PHI-free list of public emergency/
    # helpline numbers built server-side from emergency_resources (FR-041). The LLM does
    # not author it. Reuses SmsQueuedResponse.
    pass


class RegisterOptOutRequest(ToolCallRequest):
    # No fields beyond call_id: the elder + their number are derived from the JWT-scoped
    # call, never taken from the request (FR-037).
    pass


class OptOutRecordedResponse(BaseModel):
    # "opted_out" once the number is on the DNC list; flag_id is the operator-queue entry.
    status: str
    flag_id: int


class SetSpanishCallbackRequest(ToolCallRequest):
    # No fields beyond call_id: the elder is derived from the JWT-scoped call. The tool
    # records the Spanish language preference and creates a Spanish callback (FR-040).
    pass


class SpanishCallbackScheduledResponse(BaseModel):
    # "scheduled" once the language preference is recorded and the callback row created.
    status: str
    callback_id: int


class CloseFamilyTaskRequest(ToolCallRequest):
    # Contract (contracts/tools-api.md): {task_id} marks one task open->delivered.
    # task_id is OPTIONAL: the open_family_tasks builtin gives the LLM the task TEXT,
    # not ids, so omitting it marks ALL of this call's elder's open (non-safety-review)
    # tasks delivered — exactly the set that was injected into the prompt. elder_id is
    # NEVER taken from the request; it is derived from the JWT-scoped call.
    task_id: int | None = Field(default=None, gt=0)


class CloseFamilyTaskResponse(BaseModel):
    # "delivered" when >=1 task was moved open->delivered, else "noop".
    status: str
    delivered: int
