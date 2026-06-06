import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


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
    turn_index: int
    eou_delay_ms: int | None = None
    transcription_delay_ms: int | None = None
    stt_duration_ms: int | None = None
    llm_ttft_ms: int | None = None
    tts_ttfb_ms: int | None = None
    llm_completion_tokens: int | None = None
    tts_characters: int | None = None


class MetricsUsageIn(BaseModel):
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    tts_characters: int = 0
    stt_audio_seconds: float = 0.0
    session_duration_seconds: float | None = None


class LogMetricsRequest(BaseModel):
    call_id: uuid.UUID
    turns: list[TurnMetricIn] = Field(default_factory=list, max_length=500)
    usage: MetricsUsageIn = Field(default_factory=MetricsUsageIn)


class MetricsAcceptedResponse(BaseModel):
    call_id: uuid.UUID
    cost_total_usd: Decimal
