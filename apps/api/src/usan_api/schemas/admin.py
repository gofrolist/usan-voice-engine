import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from usan_api.schemas._validators import TIMEZONE_MAX_LENGTH, validate_iana_timezone


class AuditEntryOut(BaseModel):
    id: int
    actor_email: str
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    detail: dict[str, Any]
    created_at: datetime


class ContactSummary(BaseModel):
    id: uuid.UUID
    name: str
    masked_phone: str
    timezone: str
    agent_profile_id: uuid.UUID | None = None
    agent_profile_name: str | None = None


class AssignProfileRequest(BaseModel):
    # null clears the assignment (fall back to the per-direction default).
    agent_profile_id: uuid.UUID | None = None


class SetTimezoneRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)
