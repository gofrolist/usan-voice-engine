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


class CallTimeWindowSlot(BaseModel):
    """One time window: [start, end) in minutes since local midnight (oracle: TimeWindow).

    Oracle field names are ``start``/``end`` (minutes), NOT ``start_hour``/``end_hour``.
    Cross-midnight windows (start >= end) are explicitly disallowed by the oracle
    description ("must satisfy startMin < endMin"); endMin=1440 (24:00) is valid.
    ``extra="allow"`` preserves any future oracle fields.
    """

    model_config = ConfigDict(extra="allow")

    start: int = Field(ge=0, le=1440)  # minutes since midnight
    end: int = Field(ge=0, le=1440)  # minutes since midnight; 1440 = 24:00


class CallTimeWindow(BaseModel):
    """Typed echo of the RetellAI ``call_time_window`` object (oracle: CallTimeWindow).

    Oracle fields (captured 2026-06-25):
      - windows: list[TimeWindow] (required, minItems=1) — [start, end) in minutes
      - timezone: str | None — IANA tz (e.g. "America/New_York"); default is "America/Los_Angeles"
      - day: list[DayOfWeek] | None — full English day names ("Monday".."Sunday")

    Mapping gaps (documented; never silently dropped):
      1. Only windows[0] maps to the native BatchWindow (native supports one window).
         Additional windows are echoed in the response but not applied.
      2. timezone is echoed but NOT applied to the native window: BatchWindow is
         per-contact-local and has no tz field.
      3. Cross-midnight (start >= end for windows[0]) cannot be expressed in the native
         window (BatchWindow rejects start_local >= end_local); in that case the native
         window is left unset and the typed value is still echoed.
    """

    model_config = ConfigDict(extra="allow")

    windows: list[CallTimeWindowSlot] = Field(min_length=1)
    timezone: str | None = None
    # Oracle DayOfWeek enum: "Monday", "Tuesday", ..., "Sunday"
    day: list[str] | None = None


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
    call_time_window: CallTimeWindow | None = None


class BatchCallResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_call_id: str
    name: str
    from_number: str
    scheduled_timestamp: int  # epoch SECONDS — deliberate Retell-faithful exception
    total_task_count: int
    call_time_window: CallTimeWindow | None = None
