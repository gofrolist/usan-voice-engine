"""Pydantic models for the native compat-key admin endpoints (issue / list / revoke)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from usan_api.db.models import CompatApiKey
from usan_api.repositories.compat_api_keys import IssuedKey


class CompatKeyCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=200)


class CompatKeyResponse(BaseModel):
    id: uuid.UUID
    key_prefix: str
    status: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_row(cls, row: CompatApiKey) -> CompatKeyResponse:
        return cls(
            id=row.id,
            key_prefix=row.key_prefix,
            status=row.status,
            label=row.label,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
        )


class CompatKeyCreatedResponse(CompatKeyResponse):
    """Create response — includes the plaintext token, returned ONCE and never again."""

    token: str

    @classmethod
    def from_issued(cls, issued: IssuedKey) -> CompatKeyCreatedResponse:
        row = issued.row
        return cls(
            id=row.id,
            key_prefix=row.key_prefix,
            status=row.status,
            label=row.label,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            token=issued.token,
        )
