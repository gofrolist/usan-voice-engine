"""Admin/operator schemas for family contacts & tasks (US2 / T027, T032).

Request/response bodies for ``routers/admin_family.py``. These are the OPERATOR plane
(``require_admin_session``); the call-time tool plane uses ``schemas/tools.py``. Output
models read straight off the ORM rows (``from_attributes``). ``phone_e164`` is returned
in full here (the contract: "Response: full FamilyContact.") because an operator manages
the number — these endpoints are admin-only and no-store.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# E.164: a leading '+' then 1–15 digits (first non-zero). Mirrors the elder phone shape.
_E164 = r"^\+[1-9]\d{1,14}$"


class FamilyContactCreate(BaseModel):
    elder_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(pattern=_E164)
    relationship: str | None = Field(default=None, max_length=100)
    # Per-kind opt in/out (keys: "crisis"/"missed_call"/"report"); missing => opted in.
    alert_prefs: dict[str, bool] = Field(default_factory=dict)


class FamilyContactUpdate(BaseModel):
    # All optional — PATCH semantics; only set fields are applied (exclude_unset).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone_e164: str | None = Field(default=None, pattern=_E164)
    relationship: str | None = Field(default=None, max_length=100)
    alert_prefs: dict[str, bool] | None = None


class FamilyContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    elder_id: uuid.UUID
    name: str
    phone_e164: str
    relationship: str | None
    alert_prefs: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class FamilyTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    elder_id: uuid.UUID
    family_contact_id: uuid.UUID | None
    message: str
    status: str
    needs_safety_review: bool
    delivered_call_id: uuid.UUID | None
    created_at: datetime
    status_updated_at: datetime | None
    status_updated_by: str | None


class FamilyTaskPatch(BaseModel):
    # Operator transition (contract): "open" approves a held (needs_safety_review) task so
    # it can be conveyed; "closed" ends it. Other lifecycle moves are agent-driven.
    status: Literal["open", "closed"]


class FamilyReportOut(BaseModel):
    # Operator plane (BAA, session-gated): operators see the full monthly trends — the SMS
    # to family stays PHI-minimized, but the rich report lives here for the care team (US8).
    model_config = ConfigDict(from_attributes=True)

    id: int
    elder_id: uuid.UUID
    elder_name: str | None = None  # outer-join; None if the elder row was deleted
    period_month: date
    calls_completed: int
    metrics: dict[str, Any]
    narrative: str
    model_version: str  # which model wrote the narrative ("deterministic" when no LLM)
    status: str  # "sent" (family notified) or "no_contact" (operator follow-up)
    sms_message_id: uuid.UUID | None
    created_at: datetime
