"""Admin calls console response schemas (spec §4.1/§4.2).

Reuses ``TranscriptSegment``/``CallOrigin`` from ``schemas/call.py`` and the
``masked_phone`` field name from ``ContactSummary`` — one name for one concept in
the ``types/api.ts`` mirror.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from usan_api.schemas._validators import reject_nested_dynamic_vars
from usan_api.schemas.call import MAX_DYNAMIC_VARS_BYTES, CallOrigin, TranscriptSegment


class AdminCallSummary(BaseModel):
    """One row of GET /v1/admin/calls (spec §4.1).

    Deliberately absent: transcript, raw phone, recording_uri/presigned URL,
    dynamic_vars, raw idempotency_key.
    """

    id: uuid.UUID
    contact_id: uuid.UUID | None
    contact_name: str | None  # names allowed in session-gated bodies (house precedent)
    masked_phone: str  # mask_phone(): "***" + last 4, "unknown" if contact gone
    direction: str
    status: str
    origin: CallOrigin | None  # parse_origin(idempotency_key)
    attempt: int
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    end_reason: str | None
    has_recording: bool  # recording_uri IS NOT NULL
    created_at: datetime


class AdminCallDetail(AdminCallSummary):
    """GET /v1/admin/calls/{call_id} (spec §4.2).

    ``dynamic_vars``, ``error``, raw ``idempotency_key``, and ``recording_uri``
    are deliberately omitted — each is a gratuitous exposure the console never needs.
    """

    livekit_room: str | None
    parent_call_id: uuid.UUID | None
    scheduled_at: datetime | None
    answered_at: datetime | None
    recording_status: str | None
    presigned_recording_url: str | None
    recording_url_ttl_s: int | None  # clamped effective TTL when URL present
    transcript: list[TranscriptSegment]


class AdminCreateCallRequest(BaseModel):
    """Admin 'call now' body. No idempotency_key — the endpoint mints a unique
    non-reserved key server-side (origin=adhoc)."""

    contact_id: uuid.UUID
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)
    profile_override: uuid.UUID | None = None

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        reject_nested_dynamic_vars(v)
        if len(json.dumps(v)) > MAX_DYNAMIC_VARS_BYTES:
            raise ValueError(f"dynamic_vars must serialize to <= {MAX_DYNAMIC_VARS_BYTES} bytes")
        return v
