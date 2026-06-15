"""Request/response schemas for per-elder call schedules (spec §4.1).

Validation contract: the schema layer 422s anything the materializer could not
act on — empty/unknown/duplicate day names, an inverted window, or a window
that never intersects quiet hours [09:00, 21:00) (wall-clock check via
``schedule_windows.effective_window``, tz-invariant by construction). Day names
travel as the lowercase ``DAY_NAMES`` strings and are normalized to canonical
Mon-first order; the bitmask stored in ``call_schedules.days_of_week`` is
exposed via ``CreateScheduleRequest.days_mask`` and rendered back to the string
list by ``ScheduleResponse.from_model``.
"""

import json
import uuid
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from usan_api.db.models import CallSchedule
from usan_api.schedule_windows import (
    DAY_NAMES,
    days_to_mask,
    effective_window,
    mask_to_days,
)

# dynamic_vars is persisted to JSONB and relayed verbatim as LiveKit dispatch
# metadata, so schedules reuse the one canonical cap from the call schemas.
from usan_api.schemas.call import MAX_DYNAMIC_VARS_BYTES

# The GET /v1/schedules ?limit= clamp lives with the read it bounds:
# repositories/call_schedules.MAX_SCHEDULES_LIMIT (spec §4.1).

# US5: the closed morning|evening slot set; the DB CHECK (migration 0022) is the
# matching backstop. An elder may have one schedule per slot.
Slot = Literal["morning", "evening"]


def _validated_days(days: list[str]) -> list[str]:
    """Non-empty, known, duplicate-free day names, normalized to Mon-first order."""
    if len(set(days)) != len(days):
        raise ValueError("days_of_week must not contain duplicate day names")
    # days_to_mask rejects empty lists and unknown names; the round-trip
    # through mask_to_days yields the canonical Mon-first ordering.
    return mask_to_days(days_to_mask(days))


def _validated_window(start: time, end: time) -> None:
    """Window order + quiet-hours intersection (spec §6.3 enforcement point 1)."""
    if start >= end:
        raise ValueError("window_start_local must be before window_end_local")
    if effective_window(start, end) is None:
        raise ValueError("window never intersects quiet hours [09:00, 21:00)")


def _capped_vars(v: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(v)) > MAX_DYNAMIC_VARS_BYTES:
        raise ValueError(f"dynamic_vars must serialize to <= {MAX_DYNAMIC_VARS_BYTES} bytes")
    return v


class CreateScheduleRequest(BaseModel):
    elder_id: uuid.UUID
    slot: Slot = "morning"  # US5; default keeps single-slot callers unchanged
    window_start_local: time
    window_end_local: time
    days_of_week: list[str] = Field(default_factory=lambda: list(DAY_NAMES))
    enabled: bool = True
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)
    profile_override: uuid.UUID | None = None

    @field_validator("days_of_week")
    @classmethod
    def _known_days(cls, v: list[str]) -> list[str]:
        return _validated_days(v)

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _capped_vars(v)

    @model_validator(mode="after")
    def _window_order_and_quiet_hours(self) -> CreateScheduleRequest:
        _validated_window(self.window_start_local, self.window_end_local)
        return self

    @property
    def days_mask(self) -> int:
        """Bitmask for call_schedules.days_of_week (bit 0 = Mon)."""
        return days_to_mask(self.days_of_week)


class UpdateScheduleRequest(BaseModel):
    """All-optional PATCH body; merged-state revalidation happens in the router.

    ``extra="forbid"`` so a PATCH carrying ``slot`` (or any unknown key) 422s rather
    than silently no-opping: ``slot`` is immutable identity (move = delete+create),
    and a silent ignore would let a caller believe a slot move succeeded. Mirrors the
    same immutable-identity guard on custom_variables' PATCH body.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    window_start_local: time | None = None
    window_end_local: time | None = None
    days_of_week: list[str] | None = None
    dynamic_vars: dict[str, Any] | None = None
    profile_override: uuid.UUID | None = None

    @field_validator("days_of_week")
    @classmethod
    def _known_days(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validated_days(v)

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if v is None else _capped_vars(v)

    @model_validator(mode="after")
    def _window_fields_travel_together(self) -> UpdateScheduleRequest:
        if (self.window_start_local is None) != (self.window_end_local is None):
            raise ValueError("window_start_local and window_end_local must be provided together")
        if self.window_start_local is not None and self.window_end_local is not None:
            _validated_window(self.window_start_local, self.window_end_local)
        return self


class ScheduleResponse(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID
    slot: Slot  # closed enum; the DB CHECK (migration 0022) guarantees the value
    enabled: bool
    window_start_local: time
    window_end_local: time
    days_of_week: list[str]
    dynamic_vars: dict[str, Any]
    profile_override: uuid.UUID | None
    next_run_at: datetime
    last_materialized_date: date | None
    last_result: str | None
    last_result_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, s: CallSchedule) -> ScheduleResponse:
        return cls(
            id=s.id,
            elder_id=s.elder_id,
            slot=s.slot,
            enabled=s.enabled,
            window_start_local=s.window_start_local,
            window_end_local=s.window_end_local,
            days_of_week=mask_to_days(s.days_of_week),
            dynamic_vars=s.dynamic_vars,
            profile_override=s.profile_override,
            next_run_at=s.next_run_at,
            last_materialized_date=s.last_materialized_date,
            last_result=s.last_result,
            last_result_at=s.last_result_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
