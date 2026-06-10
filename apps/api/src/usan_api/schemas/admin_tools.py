"""Admin read-model summaries for the Phase-3 tool tables (design §5/§6).

`from_attributes=True` lets these validate directly from the ORM rows the
repositories return. FollowupFlagSummary intentionally exposes `reason` (PHI):
the admin endpoint is session-gated and audited (see routers/admin_tools.py).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FollowupFlagSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    elder_id: uuid.UUID
    severity: str
    category: str
    reason: str | None
    status: str
    created_at: datetime


class CallbackRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    elder_id: uuid.UUID
    requested_time_text: str
    requested_at: datetime | None
    notes: str | None
    status: str
    created_at: datetime


class SmsMessageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID
    elder_id: uuid.UUID
    to_number: str
    template_key: str
    status: str
    telnyx_message_id: str | None = None
    sent_at: datetime | None = None
    created_at: datetime
    # NOTE: the rendered `body` is intentionally OMITTED — it may carry the elder's
    # name / contextual content (design §9); summaries stay lean and lower-PHI.
