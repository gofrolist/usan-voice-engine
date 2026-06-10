"""Operator API for one-off call batches (spec §4.2, §5.6).

PHI note (spec §8): ``name`` is PHI-free BY CONVENTION — never type an elder's
name into it; it is never bound into log lines. Audit lines bind the client IP,
ids, counts, and the action only.

Replay contract: ``payload_digest`` resolves an ``idempotency_key`` collision —
same key + same digest replays the stored batch with **200** (deliberately even
after cancel: replay is a read, never a re-run); same key + different digest is
a **409** divergence. Create-time validation is all-or-nothing: every target is
checked and the 422 detail lists ``{target_index, error}`` per failure — no
batch row is persisted on any failure.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.client_ip import client_ip
from usan_api.db.models import CallBatch, Elder
from usan_api.db.session import get_db
from usan_api.observability.custom_metrics import BATCH_EVENTS_TOTAL
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import calls as calls_repo
from usan_api.schedule_windows import days_to_mask
from usan_api.schemas.batch import (
    BatchCounts,
    BatchDetailResponse,
    BatchSummaryResponse,
    BatchTargetResponse,
    CreateBatchRequest,
    payload_digest,
)

router = APIRouter(
    prefix="/v1/batches",
    tags=["batches"],
    dependencies=[Depends(require_operator_token)],
)

_OVERRIDE_ERROR = "profile_override must reference an active profile with a published version"


def _audit(request: Request, batch_id: uuid.UUID, action: str, **extra: int | str) -> None:
    """Mutation audit line (spec §4/§8): client IP + ids + counts — never the name."""
    logger.bind(client=client_ip(request), batch_id=str(batch_id), action=action, **extra).info(
        "Batch {action}", action=action
    )


async def _summary(db: AsyncSession, batch: CallBatch) -> BatchSummaryResponse:
    counts = BatchCounts(**await batches_repo.target_counts(db, batch.id))
    return BatchSummaryResponse.from_model(batch, counts)


async def _replay_or_conflict(
    db: AsyncSession, existing: CallBatch, digest: str, response: Response
) -> BatchSummaryResponse:
    """Digest replay (spec §4.2): the key selects the batch, the digest verifies
    the payload behind it. Batch STATUS is deliberately ignored — replaying a
    cancelled batch returns the cancelled batch, never a silent re-run."""
    if existing.payload_digest != digest:
        raise HTTPException(
            status_code=409, detail="idempotency_key reused with a different payload"
        )
    response.status_code = 200
    return await _summary(db, existing)


async def _validate_all_or_nothing(db: AsyncSession, body: CreateBatchRequest) -> None:
    """Every target checked, every failure reported (spec §4.2): one SELECT for
    all elder ids, then override liveness for the batch and each target; any
    error -> 422 with detail=[{target_index, error}] and nothing persisted."""
    errors: list[dict[str, Any]] = []
    result = await db.execute(
        select(Elder.id).where(Elder.id.in_([t.elder_id for t in body.targets]))
    )
    known = set(result.scalars().all())
    errors.extend(
        {"target_index": index, "error": "elder not found"}
        for index, target in enumerate(body.targets)
        if target.elder_id not in known
    )

    live: dict[uuid.UUID, bool] = {}

    async def _is_live(profile_id: uuid.UUID) -> bool:
        if profile_id not in live:
            live[profile_id] = await agent_profiles_repo.is_live_profile(db, profile_id)
        return live[profile_id]

    if body.profile_override is not None and not await _is_live(body.profile_override):
        errors.append({"target_index": "batch", "error": _OVERRIDE_ERROR})
    for index, target in enumerate(body.targets):
        if target.profile_override is not None and not await _is_live(target.profile_override):
            errors.append({"target_index": index, "error": _OVERRIDE_ERROR})
    if errors:
        raise HTTPException(status_code=422, detail=errors)


async def _get_or_404(db: AsyncSession, batch_id: uuid.UUID) -> CallBatch:
    batch = await batches_repo.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


@router.post("", status_code=201, response_model=BatchSummaryResponse)
async def create_batch(
    body: CreateBatchRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> BatchSummaryResponse:
    digest = payload_digest(body)
    if body.idempotency_key is not None:
        existing = await batches_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is not None:
            return await _replay_or_conflict(db, existing, digest, response)
    await _validate_all_or_nothing(db, body)
    window = body.window
    try:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name=body.name,
            idempotency_key=body.idempotency_key,
            payload_digest=digest,
            trigger_at=body.trigger_at,
            window_start_local=None if window is None else window.start_local,
            window_end_local=None if window is None else window.end_local,
            days_of_week=(
                None
                if window is None or window.days_of_week is None
                else days_to_mask(window.days_of_week)
            ),
            max_concurrency=body.max_concurrency,
            profile_override=body.profile_override,
            targets=body.targets,
        )
        await db.commit()
    except IntegrityError as exc:
        # UNIQUE idempotency_key race with a concurrent POST: resolve it the same
        # way as the pre-check — digest replay or 409 divergence.
        await db.rollback()
        existing = (
            None
            if body.idempotency_key is None
            else await batches_repo.get_by_idempotency_key(db, body.idempotency_key)
        )
        if existing is None:
            raise  # not the key race (e.g. an elder vanished mid-flight)
        del exc
        return await _replay_or_conflict(db, existing, digest, response)
    # Increment-after-commit (spec §7); replays above return without counting —
    # the concurrent POST that actually created the batch already counted it.
    BATCH_EVENTS_TOTAL.labels(event="created").inc()
    _audit(request, batch.id, "batch_created", targets=len(body.targets))
    return BatchSummaryResponse.from_model(batch, BatchCounts(pending=len(body.targets)))


@router.get("", response_model=list[BatchSummaryResponse])
async def list_batches(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[BatchSummaryResponse]:
    # The repository clamps limit/offset to the bounded-read house rules (<=500).
    rows = await batches_repo.list_batches(db, status=status, limit=limit, offset=offset)
    return [await _summary(db, batch) for batch in rows]


@router.get("/{batch_id}", response_model=BatchDetailResponse)
async def get_batch(batch_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> BatchDetailResponse:
    batch = await _get_or_404(db, batch_id)
    summary = await _summary(db, batch)
    # Histogram reads the denormalized final_status column — never a chain walk.
    histogram = await batches_repo.final_status_histogram(db, batch.id)
    targets = await batches_repo.list_targets(db, batch.id)
    return BatchDetailResponse(
        **summary.model_dump(),
        final_status_histogram=histogram,
        targets=[BatchTargetResponse.from_model(t) for t in targets],
    )


@router.post("/{batch_id}/cancel", response_model=BatchSummaryResponse)
async def cancel_batch(
    batch_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BatchSummaryResponse:
    """Cancel in ONE transaction (spec §5.6): batch -> cancelled, pending targets
    -> cancelled, and each materialized chain's QUEUED tip guarded-cancelled.
    In-flight calls are never torn down. Idempotent: re-cancelling returns 200
    unchanged; a completed batch is a 409."""
    batch = await _get_or_404(db, batch_id)
    was_cancelled = batch.status == "cancelled"
    try:
        root_call_ids = await batches_repo.cancel_batch(db, batch, now=datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    cancelled_calls = await calls_repo.cancel_queued_tips(db, root_call_ids)
    await db.commit()
    if not was_cancelled:
        # Increment-after-commit (spec §7); an idempotent re-cancel is not a
        # lifecycle transition and never double-counts.
        BATCH_EVENTS_TOTAL.labels(event="cancelled").inc()
    await db.refresh(batch)
    _audit(
        request,
        batch.id,
        "batch_cancelled",
        roots=len(root_call_ids),
        cancelled_calls=cancelled_calls,
    )
    return await _summary(db, batch)
