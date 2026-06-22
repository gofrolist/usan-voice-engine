"""RetellAI-compatible ``create-batch-call`` schemas (feature 003, US4).

RetellAI serves create-batch-call at the **unversioned** root path. The request
``trigger_timestamp`` is epoch **milliseconds** (consistent with every other Retell
timestamp); the response ``scheduled_timestamp`` is the ONE deliberate exception —
epoch **seconds** — kept faithful to RetellAI (asserted in the contract test).

``extra="allow"`` so unknown CRM fields are accepted and (for the request) ignored
rather than 422-ing a migrating client; the response echoes only the canonical
fields plus whatever the router sets.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Mirror the native one-batch volume cap so an oversized batch fails with a clean
# RetellAI ``{status,message}`` 422 here rather than deeper in native validation.
from usan_api.schemas.batch import MAX_BATCH_TARGETS


class BatchCallTask(BaseModel):
    """One outbound target: a destination number plus optional per-call dynamic
    variables, agent override, and CRM metadata (unknown fields echoed/ignored)."""

    model_config = ConfigDict(extra="allow")

    to_number: str
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    override_agent_id: str | None = None
    metadata: dict[str, Any] | None = None


class CreateBatchCallRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    from_number: str
    tasks: list[BatchCallTask] = Field(min_length=1, max_length=MAX_BATCH_TARGETS)
    name: str | None = None
    trigger_timestamp: int | None = Field(default=None, ge=0)  # epoch MS; omit = immediate
    reserved_concurrency: int | None = Field(default=None, ge=1)
    # Opaque echo (PENDING-FREEZE: the exact RetellAI shape + whether it maps onto the
    # native dial window is pinned against the captured oracle before the contract freezes).
    call_time_window: dict[str, Any] | None = None


class BatchCallResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_call_id: str
    name: str
    from_number: str
    scheduled_timestamp: int  # epoch SECONDS — deliberate Retell-faithful exception
    total_task_count: int
    call_time_window: dict[str, Any] | None = None
