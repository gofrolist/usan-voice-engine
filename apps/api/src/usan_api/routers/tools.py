import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import cost, sms_render
from usan_api.auth import require_service_token
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.observability.custom_metrics import (
    CALLBACK_REQUESTS_TOTAL,
    CALLS_TOTAL,
    FOLLOWUP_FLAGS_TOTAL,
    track_tool,
)
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.repositories import medications as medications_repo
from usan_api.repositories import metrics as metrics_repo
from usan_api.repositories import sms_messages as sms_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.schemas.tools import (
    CallbackScheduledResponse,
    CallEndedResponse,
    EndCallRequest,
    FlagForFollowupRequest,
    FollowupFlaggedResponse,
    GetTodayMedsRequest,
    LoggedResponse,
    LogMedicationRequest,
    LogMetricsRequest,
    LogTranscriptRequest,
    LogWellnessRequest,
    MedicationScheduleItem,
    MetricsAcceptedResponse,
    ScheduleCallbackRequest,
    SendSmsRequest,
    SmsQueuedResponse,
    TodayMedsResponse,
    TranscriptLoggedResponse,
)
from usan_api.settings import Settings, get_settings
from usan_api.sms_outbox import flush_pending_sms

router = APIRouter(prefix="/v1/tools", tags=["tools"])


async def _authorize_call(call_id: uuid.UUID, claims: dict[str, Any], db: AsyncSession) -> Call:
    """Verify the JWT is scoped to this call and load it (404 if unknown)."""
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return call


def _require_elder(call: Call) -> uuid.UUID:
    if call.elder_id is None:
        raise HTTPException(status_code=409, detail="call has no associated elder")
    return call.elder_id


@router.post("/log_wellness", response_model=LoggedResponse)
@track_tool("log_wellness")
async def log_wellness(
    body: LogWellnessRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await wellness_repo.create_wellness_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        mood=body.mood,
        pain_level=body.pain_level,
        notes=body.notes,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged wellness")
    return LoggedResponse(id=row.id)


@router.post("/flag_for_followup", response_model=FollowupFlaggedResponse)
@track_tool("flag_for_followup")
async def flag_for_followup(
    body: FlagForFollowupRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> FollowupFlaggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await follow_up_flags_repo.create_follow_up_flag(
        db,
        call_id=call.id,
        elder_id=elder_id,
        severity=body.severity,
        category=body.category,
        reason=body.reason,
    )
    await db.commit()
    # Increment AFTER commit so a crash can't double-count. Labels are bounded
    # enums only — never body.reason (free-text PHI).
    FOLLOWUP_FLAGS_TOTAL.labels(severity=body.severity, category=body.category).inc()
    # Don't log body.reason: it can carry clinical content; it's persisted to the DB.
    logger.bind(call_id=str(call.id)).info("Flagged for follow-up")
    return FollowupFlaggedResponse(id=row.id)


@router.post("/log_medication", response_model=LoggedResponse)
@track_tool("log_medication")
async def log_medication(
    body: LogMedicationRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await medications_repo.create_medication_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        medication_name=body.medication_name,
        taken=body.taken,
        reported_time=body.reported_time,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged medication")
    return LoggedResponse(id=row.id)


@router.post("/schedule_callback", response_model=CallbackScheduledResponse)
@track_tool("schedule_callback")
async def schedule_callback(
    body: ScheduleCallbackRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallbackScheduledResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await callback_requests_repo.create_callback_request(
        db,
        call_id=call.id,
        elder_id=elder_id,
        requested_time_text=body.requested_time_text,
        requested_at=body.requested_at,
        notes=body.notes,
    )
    await db.commit()
    # Increment AFTER commit so a crash mid-commit can't double-count (spec §7).
    # No label carries requested_time_text/notes (free-text PHI) — bounded by design.
    CALLBACK_REQUESTS_TOTAL.inc()
    # Don't log requested_time_text/notes: free-text the LLM fills. Already persisted to
    # the DB; the log keeps only call_id.
    logger.bind(call_id=str(call.id)).info("Scheduled callback request")
    return CallbackScheduledResponse(id=row.id)


@router.post("/get_today_meds", response_model=TodayMedsResponse)
@track_tool("get_today_meds")
async def get_today_meds(
    body: GetTodayMedsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> TodayMedsResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    elder = await elders_repo.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=409, detail="elder record not found")
    raw = elder.meta.get("medication_schedule", [])
    items: list[MedicationScheduleItem] = []
    if isinstance(raw, list):
        for entry in raw:
            try:
                items.append(MedicationScheduleItem.model_validate(entry))
            except ValidationError:
                logger.bind(elder_id=str(elder_id)).warning("Skipping malformed medication entry")
    return TodayMedsResponse(medications=items)


@router.post("/end_call", response_model=CallEndedResponse)
@track_tool("end_call")
async def end_call(
    body: EndCallRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallEndedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    updated = await calls_repo.complete_call_if_in_progress(db, call.id, end_reason=body.reason)
    await db.commit()
    final = updated or call
    if updated is not None:
        # Count only the actual terminal transition, not idempotent end_call replays.
        # Label value is the bounded call_status enum, NOT body.reason (free-text PHI).
        CALLS_TOTAL.labels(direction=updated.direction.value, end_reason=updated.status.value).inc()
        # Deliver any queued SMS after the response (own session); idempotent so the
        # room_finished webhook firing too is safe (design §6.3).
        background_tasks.add_task(flush_pending_sms, call.id)
    # Don't log body.reason: it's free-text the LLM fills, so it could carry clinical
    # content. It's already persisted to the DB (end_reason); the log keeps only call_id.
    logger.bind(call_id=str(call.id)).info("end_call requested")
    return CallEndedResponse(status=final.status.value)


@router.post("/send_sms", response_model=SmsQueuedResponse)
@track_tool("send_sms")
async def send_sms(
    body: SendSmsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> SmsQueuedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    elder = await elders_repo.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=409, detail="elder record not found")
    if not elder.phone_e164:
        raise HTTPException(status_code=409, detail="elder has no phone number")

    resolved = await profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        elder_profile_id=elder.agent_profile_id,
        direction=call.direction.value,
    )
    cfg = resolved.config if resolved is not None else DEFAULT_AGENT_CONFIG
    sms_cfg = cfg.tools.sms
    template = None
    if sms_cfg is not None:
        template = next((t for t in sms_cfg.templates if t.key == body.template_key), None)
    if template is None:
        # Either send_sms is not configured, or the key doesn't match a template.
        raise HTTPException(status_code=404, detail="sms template not found")

    rendered = sms_render.render_sms_body(template.body, call=call, elder=elder)
    row = await sms_repo.create_sms_message(
        db,
        call_id=call.id,
        elder_id=elder_id,
        to_number=elder.phone_e164,
        template_key=template.key,
        body=rendered,
    )
    await db.commit()
    # Does NOT send synchronously: flush_pending_sms delivers post-call (design §6.3).
    # Bind only call_id (matches flag_for_followup / schedule_callback); elder_id is PHI.
    logger.bind(call_id=str(call.id)).info("Queued send_sms")
    return SmsQueuedResponse(id=row.id, status=row.status)


@router.post("/log_transcript", response_model=TranscriptLoggedResponse)
@track_tool("log_transcript")
async def log_transcript(
    body: LogTranscriptRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> TranscriptLoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    count = await transcripts_repo.create_transcript_segments(
        db, call_id=call.id, segments=body.segments
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged {n} transcript segments", n=count)
    return TranscriptLoggedResponse(count=count)


@router.post("/log_metrics", response_model=MetricsAcceptedResponse)
@track_tool("log_metrics")
async def log_metrics(
    body: LogMetricsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> MetricsAcceptedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    existing = await metrics_repo.get_call_metrics(db, call.id)
    if existing is not None:
        return MetricsAcceptedResponse(call_id=call.id, cost_total_usd=existing.cost_total_usd)
    pricing = cost.Pricing.from_settings(settings)
    duration = call.duration_seconds
    if duration is None and body.usage.session_duration_seconds is not None:
        duration = round(body.usage.session_duration_seconds)
    costs = cost.compute_costs(
        duration_seconds=duration,
        llm_prompt_tokens=body.usage.llm_prompt_tokens,
        llm_completion_tokens=body.usage.llm_completion_tokens,
        tts_characters=body.usage.tts_characters,
        stt_audio_seconds=body.usage.stt_audio_seconds,
        recording_bytes=0,
        pricing=pricing,
    )
    try:
        await metrics_repo.create_metrics(
            db,
            call_id=call.id,
            turns=body.turns,
            usage=body.usage,
            costs=costs,
            duration_seconds=duration,
            pricing_version=pricing.version,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await metrics_repo.get_call_metrics(db, call.id)
        if existing is not None:
            return MetricsAcceptedResponse(call_id=call.id, cost_total_usd=existing.cost_total_usd)
        raise
    logger.bind(call_id=str(call.id)).info("Logged call metrics: {n} turns", n=len(body.turns))
    return MetricsAcceptedResponse(call_id=call.id, cost_total_usd=costs["total"])
