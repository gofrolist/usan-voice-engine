import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from usan_api.db.models import Call


class CreateCallRequest(BaseModel):
    elder_id: uuid.UUID
    idempotency_key: str = Field(min_length=1)
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)


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
