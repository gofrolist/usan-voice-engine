"""Admin read-model summaries for the Phase-3 tool tables (design §5/§6).

`from_attributes=True` lets these validate directly from the ORM rows the
repositories return. FollowupFlagSummary intentionally exposes `reason` (PHI):
the admin endpoint is session-gated and audited (see routers/admin_tools.py).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class QueueStatusUpdateRequest(BaseModel):
    """PATCH body for the ops-queue transitions (spec §4.3)."""

    status: Literal["acknowledged", "resolved"]  # "open" is not a settable target -> 422


class QueuesSummary(BaseModel):
    """PHI-free ops-queue counts backing the UI tab badges (spec §4.5).

    Counts only — never a name, phone, reason, or notes string. The serving
    endpoint is deliberately un-audited (see routers/admin_tools.py).
    """

    flags_open: int
    flags_open_urgent: int
    flags_acknowledged: int
    callbacks_open: int
    callbacks_acknowledged: int


class FollowupFlagSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    contact_id: uuid.UUID
    # C3 contact identity (spec §4.4): a nurse seeing "urgent / medical / chest
    # pain" must not need an audited transcript read just to learn WHO.
    contact_name: str | None  # outer-join; None if the contact row was deleted
    masked_phone: str  # computed by the router via masking.mask_phone — never the raw phone
    severity: str
    category: str
    reason: str | None
    status: str
    status_updated_at: datetime | None = None  # NULL = never transitioned past 'open'
    status_updated_by: str | None = None  # admin actor email; defaults keep the legacy
    # from_attributes stubs in test_admin_tools_schemas.py valid
    created_at: datetime


class CallbackRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    contact_id: uuid.UUID
    # C3 contact identity (spec §4.4); see FollowupFlagSummary.
    contact_name: str | None
    masked_phone: str  # computed by the router via masking.mask_phone — never the raw phone
    requested_time_text: str
    requested_at: datetime | None
    notes: str | None
    status: str
    # Set by the callback dialer (US8) once the request is materialized into an outbound
    # call; None for requests still in the open/acknowledged/resolved ops lane.
    dispatched_call_id: uuid.UUID | None = None
    status_updated_at: datetime | None = None  # NULL = never transitioned past 'open'
    status_updated_by: str | None = None  # admin actor email
    created_at: datetime


class SmsMessageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID
    contact_id: uuid.UUID
    to_number: str
    template_key: str
    status: str
    telnyx_message_id: str | None = None
    sent_at: datetime | None = None
    created_at: datetime
    # NOTE: the rendered `body` is intentionally OMITTED — it may carry the contact's
    # name / contextual content (design §9); summaries stay lean and lower-PHI.
