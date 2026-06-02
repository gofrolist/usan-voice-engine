import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from usan_api.db.models import Call

# Cap the serialized dynamic_vars payload: it is persisted to JSONB and relayed
# verbatim as LiveKit agent-dispatch metadata, so it must stay small.
MAX_DYNAMIC_VARS_BYTES = 8192


class CreateCallRequest(BaseModel):
    elder_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v)) > MAX_DYNAMIC_VARS_BYTES:
            raise ValueError(f"dynamic_vars must serialize to <= {MAX_DYNAMIC_VARS_BYTES} bytes")
        return v


class CallOutcomeRequest(BaseModel):
    outcome: Literal["voicemail_left"]


class CallResponse(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID | None
    direction: str
    status: str
    idempotency_key: str | None
    livekit_room: str | None
    attempt: int
    recording_uri: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, call: Call) -> CallResponse:
        return cls(
            id=call.id,
            elder_id=call.elder_id,
            direction=call.direction.value,
            status=call.status.value,
            idempotency_key=call.idempotency_key,
            livekit_room=call.livekit_room,
            attempt=call.attempt,
            recording_uri=call.recording_uri,
            created_at=call.created_at,
        )


class InboundCallRequest(BaseModel):
    phone_e164: str | None = Field(default=None, max_length=32)
    livekit_room: str = Field(min_length=1, max_length=255)
    sip_call_id: str | None = Field(default=None, max_length=255)


class InboundCallResponse(BaseModel):
    call_id: uuid.UUID
    elder_known: bool
    dynamic_vars: dict[str, Any]
