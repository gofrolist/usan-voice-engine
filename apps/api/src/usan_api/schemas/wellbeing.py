"""Request/response schemas for the wellbeing tools (US6 / T062).

``record_survey`` captures the monthly wellbeing survey (loneliness, mood, satisfaction);
each score is an optional 1-5 rating, so an out-of-scale value is rejected with 422 before
it reaches the DB (Constitution III). ``get_activity`` requests a mood-boosting activity of
an optional kind (the closed ``ActivityKindFilter`` set, default "any").
"""

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from usan_api.activities_catalog import ActivityKindFilter


class RecordSurveyRequest(BaseModel):
    call_id: uuid.UUID
    # 1-5 ratings (FR-032). All optional: the agent records whatever the elder answered;
    # an omitted score is NULL, not a forced value. ``raw`` carries any extra structured
    # detail for forward-compatibility without a schema change.
    loneliness: int | None = Field(default=None, ge=1, le=5)
    mood: int | None = Field(default=None, ge=1, le=5)
    satisfaction: int | None = Field(default=None, ge=1, le=5)
    raw: dict[str, Any] = Field(default_factory=dict)


class RecordSurveyResponse(BaseModel):
    id: int
    period_month: date


class GetActivityRequest(BaseModel):
    call_id: uuid.UUID
    # "any" (default) | "breathing" | "memory" | "game" — a closed set, so an unknown kind
    # is a 422 at the boundary rather than a silent fall-through to "any".
    kind: ActivityKindFilter = "any"


class GetActivityResponse(BaseModel):
    activity_key: str
    title: str
    script: str
