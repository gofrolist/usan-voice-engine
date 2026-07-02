"""Native admin knowledge-base schemas — raw UUIDs on the session-cookie/RLS plane,
distinct from the RetellAI-compat kb_-token surface. Source ``content`` is never echoed."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

NAME_MAX = 40
TITLE_MAX = 200


class KbSummary(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    source_count: int
    updated_at: datetime


class KbSourceOut(BaseModel):
    id: uuid.UUID
    title: str | None
    status: str  # derived: "pending" (no chunks yet) | "embedded"
    created_at: datetime


class KbDetail(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    error_detail: str | None
    sources: list[KbSourceOut]
    created_at: datetime
    updated_at: datetime


class KbCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > NAME_MAX:
            raise ValueError(f"name must be 1..{NAME_MAX} characters")
        return v


class KbSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    text: str

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > TITLE_MAX:
            raise ValueError(f"title must be 1..{TITLE_MAX} characters")
        return v

    @field_validator("text")
    @classmethod
    def _validate_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v
