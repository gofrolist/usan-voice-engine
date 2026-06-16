import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from usan_api.db.models import Contact
from usan_api.schemas._validators import (
    E164_PATTERN,
    PHONE_MAX_LENGTH,
    TIMEZONE_MAX_LENGTH,
    validate_iana_timezone,
)


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone_e164: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str | None = Field(default=None, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        # Optional: only validate a provided value; omission/None is a no-op so a
        # partial update that doesn't touch timezone is unaffected.
        return None if v is None else validate_iana_timezone(v)


class ContactResponse(BaseModel):
    id: uuid.UUID
    external_id: str | None
    name: str
    phone_e164: str
    timezone: str
    preferred_voice: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, contact: Contact) -> ContactResponse:
        return cls(
            id=contact.id,
            external_id=contact.external_id,
            name=contact.name,
            phone_e164=contact.phone_e164,
            timezone=contact.timezone,
            preferred_voice=contact.preferred_voice,
            metadata=contact.meta,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
