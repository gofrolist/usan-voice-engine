from datetime import datetime

from pydantic import BaseModel, Field

from usan_api.db.models import DNCEntry


class DNCCreate(BaseModel):
    phone_e164: str = Field(min_length=1)
    reason: str | None = None


class DNCResponse(BaseModel):
    phone_e164: str
    reason: str | None
    added_at: datetime

    @classmethod
    def from_model(cls, entry: DNCEntry) -> DNCResponse:
        return cls(phone_e164=entry.phone_e164, reason=entry.reason, added_at=entry.added_at)
