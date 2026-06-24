import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from usan_api.schemas._validators import (
    E164_PATTERN,
    PHONE_MAX_LENGTH,
    TIMEZONE_MAX_LENGTH,
    validate_iana_timezone,
)


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


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=200)
    preferred_voice: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)


class ContactUpdate(BaseModel):
    """All-optional PATCH; extra='forbid' so privileged/unknown keys 422 instead of
    silently no-opping. agent_profile_id/timezone keep their dedicated endpoints."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone_e164: str | None = Field(
        default=None, min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN
    )
    timezone: str | None = Field(default=None, min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=200)
    preferred_voice: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        return None if v is None else validate_iana_timezone(v)


class ContactDetail(ContactSummary):
    external_id: str | None = None
    preferred_voice: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
