"""Admin calls console endpoints (spec §4.1/§4.2).

Session-gated reads (viewer OK — explicit access policy, spec §1.1/§6.4). Every
list read writes a PHI-FREE audit row in the same commit: filter shape + count
only, never names/phones. B6 appends the detail GET to this file.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_session
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.masking import mask_phone
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_calls as admin_calls_repo
from usan_api.schemas.admin_calls import AdminCallSummary
from usan_api.schemas.call import parse_origin

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-calls"],
    dependencies=[Depends(require_admin_session)],
)


def _assume_utc(v: datetime | None) -> datetime | None:
    # A naive ISO query value would compare against TIMESTAMPTZ under an implicit
    # session tz. Treat tz-naive as UTC; aware values pass through unchanged
    # (house precedent: ScheduleCallbackRequest.requested_at).
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


def _summary(call: Call, elder_name: str | None, phone: str | None) -> AdminCallSummary:
    return AdminCallSummary(
        id=call.id,
        elder_id=call.elder_id,
        elder_name=elder_name,
        masked_phone=mask_phone(phone),
        direction=call.direction.value,
        status=call.status.value,
        origin=parse_origin(call.idempotency_key),
        attempt=call.attempt,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        end_reason=call.end_reason,
        has_recording=call.recording_uri is not None,
        created_at=call.created_at,
    )


@router.get("/calls", response_model=list[AdminCallSummary])
async def list_calls(
    elder_id: uuid.UUID | None = Query(default=None),
    status: CallStatus | None = Query(default=None),
    direction: CallDirection | None = Query(default=None),
    origin: Literal["schedule", "batch", "adhoc"] | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[AdminCallSummary]:
    created_from = _assume_utc(created_from)
    created_to = _assume_utc(created_to)
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(status_code=422, detail="created_from must be <= created_to")
    rows = await admin_calls_repo.list_calls(
        db,
        elder_id=elder_id,
        status=status,
        direction=direction,
        origin=origin,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    # PHI read (elder names + masked phones) -> audit. Detail carries only the
    # seven filter values + count (spec §4.1 — no `limit`), NEVER names/phones.
    # Guard the audit write+commit so a transient DB error rolls the session
    # back instead of leaving it dirty (matches admin_tools / admin_elders).
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="calls.list",
            entity_type="call",
            entity_id=None,  # a list has no single entity; the elder filter goes in detail
            detail={
                "elder_id": str(elder_id) if elder_id is not None else None,
                "status": status.value if status is not None else None,
                "direction": direction.value if direction is not None else None,
                "origin": origin,
                "created_from": created_from.isoformat() if created_from is not None else None,
                "created_to": created_to.isoformat() if created_to is not None else None,
                "offset": offset,
                "count": len(rows),
            },
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    return [_summary(call, elder_name, phone) for call, elder_name, phone in rows]
