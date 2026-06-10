"""Admin calls console endpoints (spec §4.1/§4.2).

Session-gated reads (viewer OK — explicit access policy, spec §1.1/§6.4). Every
list read writes a PHI-FREE audit row in the same commit: filter shape + count
only, never names/phones. B6 appends the detail GET to this file.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import phi_audit, recording_urls
from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_session
from usan_api.client_ip import client_ip
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.masking import mask_phone
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_calls as admin_calls_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.schemas.admin_calls import AdminCallDetail, AdminCallSummary
from usan_api.schemas.call import TranscriptSegment, parse_origin
from usan_api.settings import Settings, get_settings

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


@router.get("/calls/{call_id}", response_model=AdminCallDetail)
async def get_call_detail(
    call_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(get_actor_email),
) -> AdminCallDetail:
    """Call detail + transcript + TTL-clamped presigned recording URL (spec §4.2).

    Mirrors operator ``get_call``'s helper order; every detail GET is one audit
    row plus the locked-sink lines — per-access granularity is the point (§6).
    """
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    # Real client IP (X-Forwarded-For first hop behind Caddy), so the PHI access
    # trail names the nurse's workstation rather than the proxy container.
    client_host = client_ip(request)
    # Admin-plane TTL ceiling: a signed URL is IP-unbound (it defeats the CIDR gate
    # once issued), so exposure is capped at ADMIN_RECORDING_URL_MAX_TTL_S. The
    # helper emits the locked-sink "Recording URL accessed" line with actor bound.
    url = await recording_urls.presigned_recording_url(
        call,
        settings,
        client_host=client_host,
        actor=actor,
        max_ttl_s=recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S,
    )
    ttl_s = (
        min(settings.recording_signed_url_ttl_s, recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S)
        if url
        else None
    )
    transcript = await transcripts_repo.list_for_call(db, call_id)
    if transcript:
        # PHI access audit (spec §6.1): only when non-empty (operator-plane parity);
        # segment count, client host, and actor — never the content itself.
        phi_audit.log_transcript_accessed(
            call_id=call_id, client=client_host, actor=actor, segments=len(transcript)
        )
    elder = await elders_repo.get_elder(db, call.elder_id) if call.elder_id is not None else None
    # PHI read -> audit row in the same commit; detail carries counts/flags only,
    # never names/phones/content. Guarded so a transient DB error rolls the
    # session back instead of leaving it dirty (matches calls.list above).
    try:
        await admin_audit.record(
            db,
            actor_email=actor,
            action="calls.get",
            entity_type="call",
            entity_id=str(call_id),
            detail={
                "segments": len(transcript),
                "has_recording": call.recording_uri is not None,
            },
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise
    summary = _summary(
        call,
        elder.name if elder is not None else None,
        elder.phone_e164 if elder is not None else None,
    )
    return AdminCallDetail(
        **summary.model_dump(),
        livekit_room=call.livekit_room,
        parent_call_id=call.parent_call_id,
        scheduled_at=call.scheduled_at,
        answered_at=call.answered_at,
        recording_status=call.recording_status,
        presigned_recording_url=url,
        recording_url_ttl_s=ttl_s,
        transcript=[TranscriptSegment.from_model(t) for t in transcript],
    )
