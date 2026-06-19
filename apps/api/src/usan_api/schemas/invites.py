import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# Minimal email regex avoids the email-validator dependency EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class InviteCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    accept_url: str
    expires_at: datetime
    created_at: datetime
    invited_by: str | None = None
    # Email delivery outcome (spec 2026-06-19): None = not attempted (feature off or a
    # list read), True = emailed, False = send failed (admin falls back to copy-link).
    email_sent: bool | None = None
