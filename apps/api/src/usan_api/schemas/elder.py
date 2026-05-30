import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from usan_api.db.models import Elder
from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH


class ElderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str = Field(min_length=1, max_length=64)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ElderUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone_e164: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str | None = Field(default=None, max_length=64)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] | None = None


class ElderResponse(BaseModel):
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
    def from_model(cls, elder: Elder) -> ElderResponse:
        return cls(
            id=elder.id,
            external_id=elder.external_id,
            name=elder.name,
            phone_e164=elder.phone_e164,
            timezone=elder.timezone,
            preferred_voice=elder.preferred_voice,
            metadata=elder.meta,
            created_at=elder.created_at,
            updated_at=elder.updated_at,
        )
