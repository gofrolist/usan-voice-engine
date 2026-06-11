"""Request/response schemas for one-off call batches (spec §4.2).

Create-time contract: 1-500 targets, each elder unique within the batch (the
422 names the offending ``target_index``), per-target ``dynamic_vars`` capped
at the canonical 8 KB, optional per-elder-local window that must intersect
quiet hours [09:00, 21:00), and a naive ``trigger_at`` assumed UTC (precedent:
``ScheduleCallbackRequest._assume_utc``).

Replay contract: ``payload_digest`` is a sha256 over the canonical sorted-key
JSON of every batch-defining field (name, trigger_at, window incl. days,
max_concurrency, profile_override, and the ORDERED targets incl. vars and
overrides). The ``idempotency_key`` itself is excluded — the key selects the
batch, the digest verifies the payload behind it. Same key + same digest
-> 200 replay; same key + different digest -> 409. Strictly stronger than the
``_idempotent_replay`` precedent (spec §4.2).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, time
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from usan_api.db.models import CallBatch, CallBatchTarget
from usan_api.schedule_windows import effective_window, mask_to_days

# Day-name and dynamic_vars contracts are shared with schedules: same day codec,
# same canonical 8 KB cap (which schedule.py imports from schemas.call).
from usan_api.schemas.schedule import _capped_vars, _validated_days

# Volume-abuse cap (spec §8); 422 above. One batch is one campaign, not a firehose.
MAX_BATCH_TARGETS = 500
# Operator label; PHI-free by convention (spec §8).
MAX_BATCH_NAME_LENGTH = 200


class BatchWindow(BaseModel):
    """Optional per-elder-local dial window; ``days_of_week is None`` = any day."""

    start_local: time
    end_local: time
    days_of_week: list[str] | None = None

    @field_validator("days_of_week")
    @classmethod
    def _known_days(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validated_days(v)

    @model_validator(mode="after")
    def _order_and_quiet_hours(self) -> BatchWindow:
        """Window order + quiet-hours intersection (spec §6.3 enforcement point 1)."""
        if self.start_local >= self.end_local:
            raise ValueError("start_local must be before end_local")
        if effective_window(self.start_local, self.end_local) is None:
            raise ValueError("window never intersects quiet hours [09:00, 21:00)")
        return self


class BatchTargetIn(BaseModel):
    elder_id: uuid.UUID
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)
    profile_override: uuid.UUID | None = None

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _capped_vars(v)


class CreateBatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_BATCH_NAME_LENGTH)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    trigger_at: datetime | None = None  # None = next poll cycle
    window: BatchWindow | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    profile_override: uuid.UUID | None = None
    targets: list[BatchTargetIn] = Field(min_length=1, max_length=MAX_BATCH_TARGETS)

    @field_validator("trigger_at")
    @classmethod
    def _assume_utc(cls, v: datetime | None) -> datetime | None:
        # A naive ISO string (no offset/Z) would land in the TIMESTAMPTZ column
        # under an implicit session tz. Treat tz-naive as UTC; aware values pass
        # through unchanged (precedent: ScheduleCallbackRequest.requested_at).
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    @model_validator(mode="after")
    def _unique_elders(self) -> CreateBatchRequest:
        # Duplicate elders within one batch = N simultaneous campaigns against one
        # elder from a single payload — rejected with the offending index (spec §4.2).
        seen: dict[uuid.UUID, int] = {}
        for index, target in enumerate(self.targets):
            first = seen.setdefault(target.elder_id, index)
            if first != index:
                raise ValueError(
                    f"duplicate elder_id across targets: target_index {index} "
                    f"repeats target_index {first}"
                )
        return self


def payload_digest(req: CreateBatchRequest) -> str:
    """sha256 over the canonical sorted-key JSON of the batch-defining payload.

    Covers (name, trigger_at, window, days, max_concurrency, profile_override,
    ordered targets incl. vars/overrides); excludes ``idempotency_key``.
    Strictly stronger than the ``_idempotent_replay`` precedent (spec §4.2).
    """
    window = (
        None
        if req.window is None
        else {
            "start_local": req.window.start_local.isoformat(),
            "end_local": req.window.end_local.isoformat(),
            "days_of_week": req.window.days_of_week,
        }
    )
    canonical: dict[str, Any] = {
        "name": req.name,
        "trigger_at": None if req.trigger_at is None else req.trigger_at.isoformat(),
        "window": window,
        "max_concurrency": req.max_concurrency,
        "profile_override": None if req.profile_override is None else str(req.profile_override),
        # a JSON array: target ORDER is part of the payload (it assigns target_index)
        "targets": [
            {
                "elder_id": str(t.elder_id),
                "dynamic_vars": t.dynamic_vars,
                "profile_override": (
                    None if t.profile_override is None else str(t.profile_override)
                ),
            }
            for t in req.targets
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class BatchCounts(BaseModel):
    """Target-status tallies for the operator's primary progress view (spec §4.2)."""

    pending: int = 0
    materialized: int = 0
    done: int = 0
    skipped: int = 0
    cancelled: int = 0


class BatchTargetResponse(BaseModel):
    target_index: int
    elder_id: uuid.UUID | None
    status: str
    skip_reason: str | None
    call_id: uuid.UUID | None
    final_status: str | None
    materialized_at: datetime | None
    finalized_at: datetime | None

    @classmethod
    def from_model(cls, t: CallBatchTarget) -> BatchTargetResponse:
        return cls(
            target_index=t.target_index,
            elder_id=t.elder_id,
            status=t.status,
            skip_reason=t.skip_reason,
            call_id=t.call_id,
            final_status=t.final_status,
            materialized_at=t.materialized_at,
            finalized_at=t.finalized_at,
        )


class BatchSummaryResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    trigger_at: datetime | None
    window_start_local: time | None
    window_end_local: time | None
    days_of_week: list[str] | None
    max_concurrency: int | None
    profile_override: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    counts: BatchCounts

    @classmethod
    def from_model(cls, b: CallBatch, counts: BatchCounts) -> BatchSummaryResponse:
        return cls(
            id=b.id,
            name=b.name,
            status=b.status,
            trigger_at=b.trigger_at,
            window_start_local=b.window_start_local,
            window_end_local=b.window_end_local,
            days_of_week=None if b.days_of_week is None else mask_to_days(b.days_of_week),
            max_concurrency=b.max_concurrency,
            profile_override=b.profile_override,
            started_at=b.started_at,
            completed_at=b.completed_at,
            cancelled_at=b.cancelled_at,
            created_at=b.created_at,
            counts=counts,
        )


class BatchDetailResponse(BatchSummaryResponse):
    """Summary + per-target rows; ``final_status_histogram`` is read straight off the
    denormalized ``final_status`` column — never a retry-chain walk (spec §3.3)."""

    final_status_histogram: dict[str, int]
    targets: list[BatchTargetResponse]
