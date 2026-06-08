import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEntryOut(BaseModel):
    id: int
    actor_email: str
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    detail: dict[str, Any]
    created_at: datetime


class ElderSummary(BaseModel):
    id: uuid.UUID
    name: str
    masked_phone: str
    agent_profile_id: uuid.UUID | None = None
    agent_profile_name: str | None = None


class AssignProfileRequest(BaseModel):
    # null clears the assignment (fall back to the per-direction default).
    agent_profile_id: uuid.UUID | None = None
