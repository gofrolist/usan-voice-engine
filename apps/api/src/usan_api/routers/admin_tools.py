"""Admin endpoints for the Phase-3 tool tables (design §5/§6) + ops queues (§4.3).

Session-gated (require_admin_session). Reads that expose PHI (`follow_up_flags`)
record a PHI-FREE audit entry: only the actor + filter shape, never `reason`.
The PATCH transitions additionally require AdminRole.ADMIN (viewer -> 403) and
audit `{"from","to"}` status strings only — never reason/notes.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import CallbackRequest, FollowUpFlag
from usan_api.db.session import get_db
from usan_api.masking import mask_phone
from usan_api.observability.custom_metrics import ADMIN_QUEUE_TRANSITIONS_TOTAL
from usan_api.repositories import admin_audit
from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.repositories import sms_messages as sms_repo
from usan_api.schemas.admin_tools import (
    CallbackRequestSummary,
    FollowupFlagSummary,
    QueueStatusUpdateRequest,
    SmsMessageSummary,
)

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-tools"],
    dependencies=[Depends(require_admin_session)],
)


def _flag_summary(
    row: FollowUpFlag, elder_name: str | None, phone: str | None
) -> FollowupFlagSummary:
    # Field-by-field: masked_phone is COMPUTED (never read off an ORM row), so a
    # model_validate(row).model_copy(...) two-step cannot work (spec §4.4).
    return FollowupFlagSummary(
        id=row.id,
        call_id=row.call_id,
        elder_id=row.elder_id,
        elder_name=elder_name,
        masked_phone=mask_phone(phone),
        severity=row.severity,
        category=row.category,
        reason=row.reason,
        status=row.status,
        status_updated_at=row.status_updated_at,
        status_updated_by=row.status_updated_by,
        created_at=row.created_at,
    )


def _callback_summary(
    row: CallbackRequest, elder_name: str | None, phone: str | None
) -> CallbackRequestSummary:
    return CallbackRequestSummary(
        id=row.id,
        call_id=row.call_id,
        elder_id=row.elder_id,
        elder_name=elder_name,
        masked_phone=mask_phone(phone),
        requested_time_text=row.requested_time_text,
        requested_at=row.requested_at,
        notes=row.notes,
        status=row.status,
        status_updated_at=row.status_updated_at,
        status_updated_by=row.status_updated_by,
        created_at=row.created_at,
    )


async def _flag_response(db: AsyncSession, row: FollowUpFlag) -> FollowupFlagSummary:
    # PATCH response path: one elder lookup feeds the same summary helper the
    # list read uses, so transition responses carry elder identity too.
    elder = await elders_repo.get_elder(db, row.elder_id)
    return _flag_summary(
        row,
        elder.name if elder is not None else None,
        elder.phone_e164 if elder is not None else None,
    )


async def _callback_response(db: AsyncSession, row: CallbackRequest) -> CallbackRequestSummary:
    elder = await elders_repo.get_elder(db, row.elder_id)
    return _callback_summary(
        row,
        elder.name if elder is not None else None,
        elder.phone_e164 if elder is not None else None,
    )


@router.get("/follow-up-flags", response_model=list[FollowupFlagSummary])
async def list_follow_up_flags(
    # Typed status (spec §4.4 deliberate change): junk now 422s instead of a
    # silent 200-empty, and attacker-typed strings never reach admin_audit.detail.
    status: Literal["open", "acknowledged", "resolved"] | None = Query(default=None),
    elder_id: uuid.UUID | None = Query(default=None),
    severity: Literal["routine", "urgent"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[FollowupFlagSummary]:
    rows = await follow_up_flags_repo.list_flags(
        db, status=status, elder_id=elder_id, severity=severity, limit=limit, offset=offset
    )
    # PHI read (reason + elder identity) -> audit. Detail carries only the filter
    # shape + count, NEVER the reason text or an elder's name/phone (PHI-free;
    # spec §9). Guard the audit write+commit so a transient DB error rolls the
    # session back instead of leaving it dirty (matches admin_elders / admin_profiles).
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="follow_up_flags.list",
            entity_type="follow_up_flag",
            entity_id=str(elder_id) if elder_id is not None else None,
            detail={"status": status, "severity": severity, "offset": offset, "count": len(rows)},
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    return [_flag_summary(flag, elder_name, phone) for flag, elder_name, phone in rows]


@router.get("/callback-requests", response_model=list[CallbackRequestSummary])
async def list_callback_requests(
    # Typed status (spec §4.4 deliberate change): junk now 422s instead of a
    # silent 200-empty, symmetric with list_follow_up_flags above.
    status: Literal["open", "acknowledged", "resolved"] | None = Query(default=None),
    elder_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[CallbackRequestSummary]:
    # Paged + status/elder-filtered in SQL (never select the whole table). Callback notes
    # are PHI but stay in our DB; this endpoint is session-gated via the router dependency.
    rows = await callback_requests_repo.list_callback_requests(
        db, status=status, elder_id=elder_id, limit=limit, offset=offset
    )
    # PHI read (notes + elder identity) -> audit. Detail carries only the filter
    # shape + count, NEVER the notes text or an elder's name/phone (PHI-free;
    # spec §9). Guard the audit write+commit so a transient DB error rolls the
    # session back instead of leaving it dirty (matches list_follow_up_flags above).
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="callback_requests.list",
            entity_type="callback_request",
            entity_id=str(elder_id) if elder_id is not None else None,
            detail={"status": status, "offset": offset, "count": len(rows)},
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    return [_callback_summary(req, elder_name, phone) for req, elder_name, phone in rows]


@router.get("/sms-messages", response_model=list[SmsMessageSummary])
async def list_sms_messages(
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[SmsMessageSummary]:
    rows = await sms_repo.list_messages(db, status=status, limit=limit)
    # PHI read -> audit. The summary omits `body`, but `to_number` is the elder's
    # E.164 phone number, which is direct PII/PHI under §10's access-logging rule,
    # so this read is audited just like the PHI-returning sibling endpoints.
    # Detail carries only the filter shape + count, NEVER the phone or body
    # (PHI-free; spec §9). Guard the write+commit so a transient DB error rolls
    # the session back instead of leaving it dirty (matches the siblings above).
    # NOTE: no `elder_id` filter here, unlike list_follow_up_flags /
    # list_callback_requests — per-elder SMS scoping is deferred because the D6
    # `sms_repo.list_messages` repo does not yet expose an `elder_id` parameter.
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="sms_messages.list",
            entity_type="sms_message",
            entity_id=None,
            detail={"status": status, "count": len(rows)},
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    return [SmsMessageSummary.model_validate(r) for r in rows]


# --- Ops-queue status transitions (spec §4.3) ---------------------------------
#
# Order of operations: read first — the pre-read serves the early 404 and the
# audit "from"; the repo's status-guarded UPDATE (WHERE clause IS the state
# machine) still owns transition correctness under races. On zero updated rows,
# RE-READ before disambiguating: a racing request may have moved the status
# between the pre-read and the UPDATE — disambiguating on the stale pre-read
# would mislabel a lost ack/ack race as a 409. Under a lost race the audited
# "from" may lag one hop behind the true predecessor — acceptable: the guarded
# UPDATE, not the audit detail, owns correctness.


@router.patch("/follow-up-flags/{flag_id}", response_model=FollowupFlagSummary)
async def update_follow_up_flag(
    flag_id: int,
    body: QueueStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> FollowupFlagSummary:
    current = await follow_up_flags_repo.get_flag(db, flag_id)  # pre-read: 404 + audit "from"
    if current is None:
        raise HTTPException(status_code=404, detail="flag not found")
    from_status = current.status
    row = await follow_up_flags_repo.update_status(
        db, flag_id, new_status=body.status, actor_email=actor
    )
    if row is None:
        fresh = await follow_up_flags_repo.get_flag(db, flag_id)  # re-read: races move status
        if fresh is None:
            raise HTTPException(status_code=404, detail="flag not found")
        if fresh.status == body.status:
            # Idempotent 200 no-op; no audit row, no metric, stamps untouched.
            return await _flag_response(db, fresh)
        logger.bind(
            flag_id=flag_id, actor=actor, from_status=fresh.status, to_status=body.status
        ).warning("Illegal queue transition")  # two humans racing
        raise HTTPException(
            status_code=409, detail=f"illegal transition: {fresh.status} -> {body.status}"
        )
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="follow_up_flag.update",
            entity_type="follow_up_flag",
            entity_id=str(flag_id),
            detail={"from": from_status, "to": body.status},  # status strings ONLY — never reason
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    ADMIN_QUEUE_TRANSITIONS_TOTAL.labels(queue="follow_up_flag", to_status=body.status).inc()
    logger.bind(flag_id=flag_id, actor=actor, to_status=body.status).info("Queue item transitioned")
    return await _flag_response(db, row)


@router.patch("/callback-requests/{request_id}", response_model=CallbackRequestSummary)
async def update_callback_request(
    request_id: int,
    body: QueueStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> CallbackRequestSummary:
    current = await callback_requests_repo.get_request(db, request_id)  # pre-read: 404 + "from"
    if current is None:
        raise HTTPException(status_code=404, detail="request not found")
    from_status = current.status
    row = await callback_requests_repo.update_status(
        db, request_id, new_status=body.status, actor_email=actor
    )
    if row is None:
        fresh = await callback_requests_repo.get_request(db, request_id)  # re-read after race
        if fresh is None:
            raise HTTPException(status_code=404, detail="request not found")
        if fresh.status == body.status:
            # Idempotent 200 no-op; no audit row, no metric, stamps untouched.
            return await _callback_response(db, fresh)
        logger.bind(
            request_id=request_id, actor=actor, from_status=fresh.status, to_status=body.status
        ).warning("Illegal queue transition")  # two humans racing
        raise HTTPException(
            status_code=409, detail=f"illegal transition: {fresh.status} -> {body.status}"
        )
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="callback_request.update",
            entity_type="callback_request",
            entity_id=str(request_id),
            detail={"from": from_status, "to": body.status},  # status strings ONLY — never notes
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    ADMIN_QUEUE_TRANSITIONS_TOTAL.labels(queue="callback_request", to_status=body.status).inc()
    logger.bind(request_id=request_id, actor=actor, to_status=body.status).info(
        "Queue item transitioned"
    )
    return await _callback_response(db, row)
