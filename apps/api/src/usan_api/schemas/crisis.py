"""Request/response schemas for the crisis-escalation tool (US1 / FR-001..FR-006).

``raise_crisis`` is called by BOTH the LLM (detection_source="llm") and the agent's
deterministic safety-net matcher (detection_source="safety_net"). The response carries
the emergency resource for the agent to speak.
"""

from typing import Literal

from pydantic import BaseModel, Field

from usan_api.schemas.tools import ToolCallRequest

# The five crisis categories (data-model.md / contracts/tools-api.md). Kept a Literal so
# the API rejects an off-enum value at the boundary; mirrored in services/agent.
CrisisCategory = Literal["suicidal", "medical", "abuse", "confusion", "overdose"]
DetectionSource = Literal["llm", "safety_net"]


class RaiseCrisisRequest(ToolCallRequest):
    category: CrisisCategory
    detection_source: DetectionSource
    # Optional short, non-PHI marker/quote — capped. Never required.
    evidence: str | None = Field(default=None, max_length=500)


class RaiseCrisisResponse(BaseModel):
    flag_id: int
    resource_label: str
    resource_number: str
    spoken_script: str
