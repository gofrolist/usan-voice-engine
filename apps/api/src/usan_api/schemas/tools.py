import uuid
from datetime import datetime
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
