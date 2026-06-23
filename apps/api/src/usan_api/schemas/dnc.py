from datetime import datetime

from pydantic import BaseModel, Field

from usan_api.db.models import DNCEntry
from usan_api.masking import mask_phone
from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH


class DNCCreate(BaseModel):
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    reason: str | None = Field(default=None, max_length=1000)


class DNCResponse(BaseModel):
    phone_e164: str
    reason: str | None
    added_at: datetime

    @classmethod
    def from_model(cls, entry: DNCEntry) -> DNCResponse:
        return cls(phone_e164=entry.phone_e164, reason=entry.reason, added_at=entry.added_at)


class AdminDNCResponse(BaseModel):
    """Admin-plane DNC row — masked phone only (spec §6.3)."""

    masked_phone: str
    reason: str | None
    added_at: datetime

    @classmethod
    def from_model(cls, entry: DNCEntry) -> AdminDNCResponse:
        return cls(
            masked_phone=mask_phone(entry.phone_e164), reason=entry.reason, added_at=entry.added_at
        )
