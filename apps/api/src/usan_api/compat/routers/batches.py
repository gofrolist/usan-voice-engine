"""RetellAI-compatible batch endpoint (feature 003, US4):

  POST /create-batch-call   (201) — UNVERSIONED root path

RetellAI serves create-batch-call at the bare root (NOT under ``/v2``), so a CRM
repointing only its base URL reaches it here. Auth + org-scoped RLS session via
``get_compat_db``; one PHI-free audit line per create (org id + batch id + task
count — never a number or a name).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import batch_create, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.batch import BatchCallResponse, CreateBatchCallRequest
from usan_api.db.models import CallBatch
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-batches"])


def _audit(request: Request, batch_id: uuid.UUID, total: int) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(
        compat_org_id=org, op="create-batch-call", batch_id=str(batch_id), total=total
    ).info("compat batch op=create-batch-call")


def _org_id(request: Request) -> uuid.UUID:
    # get_compat_db stashed it before the handler ran; always present here.
    return uuid.UUID(request.state.compat_org_id)


def _serialize(batch: CallBatch, body: CreateBatchCallRequest) -> BatchCallResponse:
    """Build the RetellAI batch object. ``scheduled_timestamp`` is Unix **seconds**
    (the one deliberate exception to the ms rule): the scheduled run time
    (``trigger_at``), or the creation time for an immediate batch."""
    scheduled_at = batch.trigger_at or batch.created_at
    return BatchCallResponse(
        batch_call_id=ids.encode_batch_id(batch.id),
        name=batch.name,
        # from_number is echoed (the engine dials from its configured trunk, not a
        # per-batch caller id) — accepted + reflected, like RetellAI.
        from_number=body.from_number,
        scheduled_timestamp=int(scheduled_at.timestamp()),
        total_task_count=len(body.tasks),
        call_time_window=body.call_time_window,
    )


@router.post(
    "/create-batch-call",
    status_code=status.HTTP_201_CREATED,
    response_model=BatchCallResponse,
)
async def create_batch_call(
    body: CreateBatchCallRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> BatchCallResponse:
    batch = await batch_create.create_compat_batch(
        db, settings, body, organization_id=_org_id(request)
    )
    response.status_code = status.HTTP_201_CREATED
    _audit(request, batch.id, len(body.tasks))
    return _serialize(batch, body)
