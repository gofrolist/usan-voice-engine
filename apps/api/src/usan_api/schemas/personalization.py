"""Request/response schemas for the personalization (memory) tools (US4 / T047).

``record_personal_fact`` captures a durable fact the contact stated during the call.
``category`` is a CLOSED set (mirrors the ``personal_facts`` CHECK constraint), so an
off-enum value is rejected with 422 before it can reach the DB (Constitution III).
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

# Closed set, identical to the personal_facts.category CHECK in migration 0021.
FactCategory = Literal["person", "routine", "preference", "important_date", "health_context"]


class RecordPersonalFactRequest(BaseModel):
    call_id: uuid.UUID
    category: FactCategory
    content: str = Field(min_length=1, max_length=2000)
    # Optional machine-readable detail, e.g. an important_date's
    # {"date": "2026-07-04", "label": "birthday"}. Defaults to an empty object.
    structured: dict[str, Any] = Field(default_factory=dict)


class RecordPersonalFactResponse(BaseModel):
    id: int
