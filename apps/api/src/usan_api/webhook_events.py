"""Outbound webhook payload builders — **the only place payloads are constructed**.

Allowlist by construction (spec §6.1): each event's ``data`` is a frozen Pydantic
model, so a field that is not on the model cannot leak. Excluded everywhere,
deliberately (spec §6.1): ``end_reason`` (conditionally free text), flag
``reason``/``category``/``elder_id`` (§6.4 — the health-domain pairing), callback
``requested_time_text``/``notes``, ``dynamic_vars``, the ``error`` JSONB, names,
phone numbers, ``livekit_room``/``sip_call_id``/``recording_uri``/``egress_id``,
batch ``name`` (PHI-free only by convention), and the raw ``idempotency_key``
(operator free text — only the parsed bounded ``origin`` egresses).

``origin`` is derived from the CHAIN ROOT's ``idempotency_key`` (spec §6.1):
retry children carry no key of their own (``schedule_retry`` creates them without
one), so the builders walk ``parent_call_id`` to the root and parse the root's
key — origin therefore describes the chain's origin on **every** attempt; it is
``None`` only for operator one-off and inbound calls.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Call, CallbackRequest, CallBatch, CallBatchTarget, FollowUpFlag
from usan_api.repositories import call_batches
from usan_api.schemas.call import CallOrigin, parse_origin

# Closed enum of subscribable events — the single source for schema validation.
# `ping` is deliberately NOT here: it is deliverable (the /test pipeline) but
# never subscribable (spec §6.7 / migration 0014's CHECK asymmetry).
WEBHOOK_EVENTS = (
    "call.started",
    "call.completed",
    "flag.created",
    "callback.created",
    "batch.completed",
)

# Mirrors repositories/calls.py: retry ladders top out at 3 attempts, so a chain
# is root + 2 children; one extra hop of headroom keeps the walk bounded. Local
# copy — calls.py imports this module for enqueue, so importing it back would
# create a cycle.
_MAX_CHAIN_HOPS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _CallStartedData(BaseModel):
    """Spec §6.2 — exactly these fields."""

    model_config = ConfigDict(frozen=True)

    call_id: uuid.UUID
    elder_id: uuid.UUID | None
    direction: str
    attempt: int
    parent_call_id: uuid.UUID | None
    origin: CallOrigin | None
    answered_at: datetime | None


class _CallCompletedData(BaseModel):
    """Spec §6.3 — exactly these fields; nullable trio for lifecycles that never
    reached them (e.g. dnc_blocked at birth)."""

    model_config = ConfigDict(frozen=True)

    call_id: uuid.UUID
    elder_id: uuid.UUID | None
    direction: str
    status: str
    attempt: int
    parent_call_id: uuid.UUID | None
    origin: CallOrigin | None
    created_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None


class _FlagCreatedData(BaseModel):
    """Spec §6.4 — deliberately reduced: NO ``elder_id``, NO ``category``, NO
    ``reason``. The care system pages on ``severity`` and resolves who/what via
    the authenticated API."""

    model_config = ConfigDict(frozen=True)

    flag_id: int
    call_id: uuid.UUID
    severity: str
    created_at: datetime


class _CallbackCreatedData(BaseModel):
    """Spec §6.5 — ``requested_at`` is the parsed timestamp only;
    ``requested_time_text``/``notes`` excluded. ``elder_id`` stays: a callback
    carries no health content (the §6.4 line is health-info x person-identifier)."""

    model_config = ConfigDict(frozen=True)

    callback_id: int
    call_id: uuid.UUID
    elder_id: uuid.UUID
    requested_at: datetime | None
    created_at: datetime


class _BatchCompletedData(BaseModel):
    """Spec §6.6 — histogram keys are the bounded call statuses; batch ``name``
    excluded (§6.1)."""

    model_config = ConfigDict(frozen=True)

    batch_id: uuid.UUID
    status: str
    target_count: int
    final_status_histogram: dict[str, int]
    completed_at: datetime | None


class _PingData(BaseModel):
    """Spec §6.7 — test deliveries only; not subscribable."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: uuid.UUID


def _envelope(event: str, data: BaseModel) -> dict[str, Any]:
    """Spec §6.1 stored envelope: exactly ``{event, occurred_at, data}`` —
    ``delivery_id`` is injected per-row at send time, before signing."""
    return {
        "event": event,
        "occurred_at": _utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "data": data.model_dump(mode="json"),
    }


async def chain_root_origin(db: AsyncSession, call: Call) -> CallOrigin | None:
    """Walk ``parent_call_id`` to the chain root (<= ``_MAX_CHAIN_HOPS``, the
    ``schedule_retry`` bound; each hop a single indexed probe), then parse the
    ROOT's ``idempotency_key`` — origin describes the chain's origin on every
    attempt (spec §6.1). ``None`` for operator one-off and inbound calls."""
    root = call
    for _ in range(_MAX_CHAIN_HOPS):
        if root.parent_call_id is None:
            break
        parent = await db.get(Call, root.parent_call_id)
        if parent is None:
            break
        root = parent
    return parse_origin(root.idempotency_key)


async def call_started_payload(db: AsyncSession, call: Call) -> dict[str, Any]:
    data = _CallStartedData(
        call_id=call.id,
        elder_id=call.elder_id,
        direction=call.direction.value,
        attempt=call.attempt,
        parent_call_id=call.parent_call_id,
        origin=await chain_root_origin(db, call),
        answered_at=call.answered_at,
    )
    return _envelope("call.started", data)


async def call_completed_payload(db: AsyncSession, call: Call) -> dict[str, Any]:
    data = _CallCompletedData(
        call_id=call.id,
        elder_id=call.elder_id,
        direction=call.direction.value,
        status=call.status.value,
        attempt=call.attempt,
        parent_call_id=call.parent_call_id,
        origin=await chain_root_origin(db, call),
        created_at=call.created_at,
        answered_at=call.answered_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
    )
    return _envelope("call.completed", data)


def flag_created_payload(flag: FollowUpFlag) -> dict[str, Any]:
    # NO elder_id, NO category, NO reason (§6.4 deliberate reduction).
    data = _FlagCreatedData(
        flag_id=flag.id,
        call_id=flag.call_id,
        severity=flag.severity,
        created_at=flag.created_at,
    )
    return _envelope("flag.created", data)


def callback_created_payload(row: CallbackRequest) -> dict[str, Any]:
    data = _CallbackCreatedData(
        callback_id=row.id,
        call_id=row.call_id,
        elder_id=row.elder_id,
        requested_at=row.requested_at,
        created_at=row.created_at,
    )
    return _envelope("callback.created", data)


async def batch_completed_payload(db: AsyncSession, batch: CallBatch) -> dict[str, Any]:
    # target_count is not a CallBatch column (§6.6): COUNT(*) over the batch's
    # targets at enqueue time, alongside the same denormalized final_status
    # aggregate the read API uses (call_batches.final_status_histogram).
    result = await db.execute(
        select(func.count())
        .select_from(CallBatchTarget)
        .where(CallBatchTarget.batch_id == batch.id)
    )
    data = _BatchCompletedData(
        batch_id=batch.id,
        status=batch.status,
        target_count=int(result.scalar_one()),
        final_status_histogram=await call_batches.final_status_histogram(db, batch.id),
        completed_at=batch.completed_at,
    )
    return _envelope("batch.completed", data)


def ping_payload(endpoint_id: uuid.UUID) -> dict[str, Any]:
    return _envelope("ping", _PingData(endpoint_id=endpoint_id))
