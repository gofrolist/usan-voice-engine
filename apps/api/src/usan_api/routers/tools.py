import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import activities_catalog, cost, emergency_resources, notifications, sms_render
from usan_api.auth import require_service_token
from usan_api.compat.kb_retrieval import RetrievedContext, retrieve_context
from usan_api.db.base import CallType
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.observability.custom_metrics import (
    CALLBACK_REQUESTS_TOTAL,
    CALLS_TOTAL,
    FOLLOWUP_FLAGS_TOTAL,
    track_tool,
)
from usan_api.ratelimit import tool_call_within_limit
from usan_api.repositories import activity_history as activity_history_repo
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.repositories import medication_reminders as medication_reminders_repo
from usan_api.repositories import medications as medications_repo
from usan_api.repositories import metrics as metrics_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import sms_messages as sms_repo
from usan_api.repositories import survey_results as survey_results_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.schemas.crisis import RaiseCrisisRequest, RaiseCrisisResponse
from usan_api.schemas.personalization import (
    RecordPersonalFactRequest,
    RecordPersonalFactResponse,
)
from usan_api.schemas.tools import (
    CallbackScheduledResponse,
    CallEndedResponse,
    CloseFamilyTaskRequest,
    CloseFamilyTaskResponse,
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
    OptOutRecordedResponse,
    RegisterOptOutRequest,
    RetrieveKbContextRequest,
    RetrieveKbContextResponse,
    ScheduleCallbackRequest,
    SendInfoSmsRequest,
    SendSmsRequest,
    SetSpanishCallbackRequest,
    SmsQueuedResponse,
    SpanishCallbackScheduledResponse,
    TodayMedsResponse,
    TranscriptLoggedResponse,
)
from usan_api.schemas.wellbeing import (
    GetActivityRequest,
    GetActivityResponse,
    RecordSurveyRequest,
    RecordSurveyResponse,
)
from usan_api.settings import Settings, get_settings
from usan_api.sms_outbox import flush_pending_sms
from usan_api.summarization import summarize_call


def _enforce_tool_call_rate(
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> None:
    """Per-call_id ceiling on the tool plane (security review): bounds a runaway/looping
    or hijacked agent token without throttling a legitimate call. require_service_token is
    FastAPI-cached, so this reuses the same decoded claims the route handler receives.
    """
    call_id = str(claims.get("call_id", ""))
    if not tool_call_within_limit(call_id, settings.tool_call_rate):
        raise HTTPException(
            status_code=429,
            detail="tool call rate limit exceeded for this call",
        )


router = APIRouter(
    prefix="/v1/tools",
    tags=["tools"],
    dependencies=[Depends(_enforce_tool_call_rate)],
)


async def _authorize_call(call_id: uuid.UUID, claims: dict[str, Any], db: AsyncSession) -> Call:
    """Verify the JWT is scoped to this call and load it (404 if unknown)."""
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return call


def _require_contact(call: Call) -> uuid.UUID:
    if call.contact_id is None:
        raise HTTPException(status_code=409, detail="call has no associated contact")
    return call.contact_id


@router.post("/log_wellness", response_model=LoggedResponse)
@track_tool("log_wellness")
async def log_wellness(
    body: LogWellnessRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    row = await wellness_repo.create_wellness_log(
        db,
        call_id=call.id,
        contact_id=contact_id,
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
    contact_id = _require_contact(call)
    row = await follow_up_flags_repo.create_follow_up_flag(
        db,
        call_id=call.id,
        contact_id=contact_id,
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
    contact_id = _require_contact(call)
    row = await medications_repo.create_medication_log(
        db,
        call_id=call.id,
        contact_id=contact_id,
        medication_name=body.medication_name,
        taken=body.taken,
        reported_time=body.reported_time,
    )
    # US3 (FR-005): maintain the re-reminder state machine alongside the immutable log.
    capped = False
    if body.taken:
        await medication_reminders_repo.clear_pending(
            db, contact_id=contact_id, medication_name=body.medication_name, call_id=call.id
        )
    else:
        _reminder, capped = await medication_reminders_repo.open_or_refresh(
            db, contact_id=contact_id, medication_name=body.medication_name, call_id=call.id
        )
        if capped:
            # Cap reached: stop nagging and hand persistent non-adherence to an operator via
            # a routine flag. reason names the med (clinical, persisted to our BAA DB only —
            # the flag.created webhook payload omits it, spec §6.4).
            await follow_up_flags_repo.create_follow_up_flag(
                db,
                call_id=call.id,
                contact_id=contact_id,
                severity="routine",
                category="medication",
                reason=(
                    f"Medication re-reminders for '{body.medication_name}' reached the cap; "
                    "needs human follow-up (US3 / FR-005)."
                ),
            )
    await db.commit()
    if capped:
        # AFTER commit so a crash can't double-count. Labels are bounded enums only.
        FOLLOWUP_FLAGS_TOTAL.labels(severity="routine", category="medication").inc()
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
    contact_id = _require_contact(call)
    row = await callback_requests_repo.create_callback_request(
        db,
        call_id=call.id,
        contact_id=contact_id,
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
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=409, detail="contact record not found")
    raw = contact.meta.get("medication_schedule", [])
    items: list[MedicationScheduleItem] = []
    if isinstance(raw, list):
        for entry in raw:
            try:
                items.append(MedicationScheduleItem.model_validate(entry))
            except ValidationError:
                logger.bind(contact_id=str(contact_id)).warning(
                    "Skipping malformed medication entry"
                )
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
        # Web calls (call_type=WEB_CALL) are excluded: direction is a placeholder for
        # them (call_type is authoritative), so incrementing by direction would
        # miscount them as inbound phone calls.
        if updated.call_type != CallType.WEB_CALL:
            CALLS_TOTAL.labels(
                direction=updated.direction.value, end_reason=updated.status.value
            ).inc()
        # Deliver any queued SMS after the response (own session); idempotent so the
        # room_finished webhook firing too is safe (design §6.3).
        background_tasks.add_task(flush_pending_sms, call.id)
        # Summarize for next-time memory (US4); flag-gated + idempotent per call, so the
        # room_finished webhook firing this too is safe (design §memory).
        background_tasks.add_task(summarize_call, call.id)
    # Don't log body.reason: it's free-text the LLM fills, so it could carry clinical
    # content. It's already persisted to the DB (end_reason); the log keeps only call_id.
    logger.bind(call_id=str(call.id)).info("end_call requested")
    return CallEndedResponse(status=final.status.value)


# Hard per-call ceiling on queued texts, counted across ALL statuses (sent rows
# spend the budget too). The /v1/tools/* paths are exempt from the global rate
# limiter, so without this a confused or hijacked LLM could queue unbounded
# carrier traffic to the contact's phone within a single call.
MAX_SMS_PER_CALL = 3


@router.post("/send_sms", response_model=SmsQueuedResponse)
@track_tool("send_sms")
async def send_sms(
    body: SendSmsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> SmsQueuedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=409, detail="contact record not found")
    if not contact.phone_e164:
        raise HTTPException(status_code=409, detail="contact has no phone number")
    if await sms_repo.count_for_call(db, call.id) >= MAX_SMS_PER_CALL:
        raise HTTPException(status_code=409, detail="per-call SMS limit reached")

    resolved = await profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact.agent_profile_id,
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

    rendered = sms_render.render_sms_body(template.body, call=call, contact=contact)
    row = await sms_repo.create_sms_message(
        db,
        call_id=call.id,
        contact_id=contact_id,
        to_number=contact.phone_e164,
        template_key=template.key,
        body=rendered,
    )
    await db.commit()
    # Does NOT send synchronously: flush_pending_sms delivers post-call (design §6.3).
    # Bind only call_id (matches flag_for_followup / schedule_callback); contact_id is PHI.
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


@router.post("/raise_crisis", response_model=RaiseCrisisResponse)
@track_tool("raise_crisis")
async def raise_crisis(
    body: RaiseCrisisRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> RaiseCrisisResponse:
    """Record a crisis escalation and return the resource script for the agent to speak.

    Called by BOTH the LLM and the deterministic safety-net matcher. Idempotent per
    (call_id, category): a second call (e.g. both paths firing) merges detection_source
    to 'both' and re-enqueues nothing new (the family alert dedupe key is stable).
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    resource = emergency_resources.get_resource(body.category)
    flag, created = await follow_up_flags_repo.upsert_crisis_flag(
        db,
        call_id=call.id,
        contact_id=contact_id,
        crisis_category=body.category,
        detection_source=body.detection_source,
        resource_offered=resource.category,
    )
    # PHI-minimized family alert to every registered contact opted in to crisis alerts
    # (US2 / T034; idempotent per flag+recipient; the body carries NO clinical detail —
    # Constitution II). When no family contact is registered, the urgent flag itself is the
    # operator-queue entry and is annotated to surface the absence (FR-013 / T088).
    dispatch = await notifications.dispatch_family_alert(
        db, contact_id=contact_id, reason="crisis", dedupe_base=f"crisis:{flag.id}"
    )
    if dispatch.notified:
        await follow_up_flags_repo.mark_family_notified(db, flag.id)
    elif not dispatch.had_contacts:
        await follow_up_flags_repo.set_no_family_contact_reason(db, flag.id)
    await db.commit()
    # Count only a NEW crisis flag, after commit — never re-count an idempotent re-raise.
    # Labels are the bounded urgent/safety enums (no PHI), like flag_for_followup.
    if created:
        FOLLOWUP_FLAGS_TOTAL.labels(severity="urgent", category="safety").inc()
    logger.bind(call_id=str(call.id)).info("Raised crisis escalation")
    return RaiseCrisisResponse(
        flag_id=flag.id,
        resource_label=resource.label,
        resource_number=resource.number,
        spoken_script=resource.spoken_script,
    )


@router.post("/close_family_task", response_model=CloseFamilyTaskResponse)
@track_tool("close_family_task")
async def close_family_task(
    body: CloseFamilyTaskRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CloseFamilyTaskResponse:
    """Mark conveyed family task(s) delivered (US2 / FR-009; ``open -> delivered``).

    The agent calls this after relaying the family's open messages. With a ``task_id`` it
    delivers that one task (contact-scoped); without one it delivers every open,
    non-safety-review task for the call's contact — the exact set injected as the
    ``open_family_tasks`` builtin. Idempotent: a re-call after delivery is a no-op.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    if body.task_id is not None:
        task = await family_tasks_repo.get_family_task(db, body.task_id)
        # Contact-scope guard: a call may only touch its own contact's tasks (no cross-contact
        # mutation from a confused/compromised agent). Hide existence on mismatch -> 404.
        if task is None or task.contact_id != contact_id:
            raise HTTPException(status_code=404, detail="family task not found")
        updated = await family_tasks_repo.mark_delivered(db, body.task_id, call_id=call.id)
        delivered = 1 if updated is not None else 0
    else:
        rows = await family_tasks_repo.mark_all_delivered(
            db, contact_id=contact_id, call_id=call.id
        )
        delivered = len(rows)
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Closed {n} family task(s)", n=delivered)
    return CloseFamilyTaskResponse(status="delivered" if delivered else "noop", delivered=delivered)


@router.post("/record_personal_fact", response_model=RecordPersonalFactResponse)
@track_tool("record_personal_fact")
async def record_personal_fact(
    body: RecordPersonalFactRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> RecordPersonalFactResponse:
    """Capture a durable fact the contact stated this call (US4 / FR-024).

    Always writes ``source='contact_stated'`` — the tool never forges an 'extracted' or
    'operator' fact. An empty ``structured`` takes the DB default ``{}``; ``phi`` takes
    its DB default (true) so a fact is protected unless an operator later proves otherwise.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    row = await personal_facts_repo.create(
        db,
        contact_id=contact_id,
        category=body.category,
        content=body.content,
        structured=body.structured or None,
        source="contact_stated",
    )
    await db.commit()
    # Bind only call_id (matches the other tools); the fact content is PHI and is already
    # persisted to the DB — never log it.
    logger.bind(call_id=str(call.id)).info("Recorded personal fact")
    return RecordPersonalFactResponse(id=row.id)


@router.post("/record_survey", response_model=RecordSurveyResponse)
@track_tool("record_survey")
async def record_survey(
    body: RecordSurveyRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> RecordSurveyResponse:
    """Record this month's wellbeing survey (US6 / FR-032; once per contact per month).

    The unique ``(contact_id, period_month)`` makes a repeat the same month a no-op that
    returns the existing row (SC-008), so the agent re-asking is harmless. ``period_month``
    is anchored to the contact's local month.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=409, detail="contact record not found")
    period = survey_results_repo.month_anchor(contact.timezone or "", datetime.now(UTC))
    row, _created = await survey_results_repo.upsert_for_month(
        db,
        call_id=call.id,
        contact_id=contact_id,
        period_month=period,
        loneliness=body.loneliness,
        mood=body.mood,
        satisfaction=body.satisfaction,
        raw=body.raw,
    )
    await db.commit()
    # Bind only call_id — the survey scores are PHI and are already persisted to the DB.
    logger.bind(call_id=str(call.id)).info("Recorded wellbeing survey")
    return RecordSurveyResponse(id=row.id, period_month=row.period_month)


@router.post("/get_activity", response_model=GetActivityResponse)
@track_tool("get_activity")
async def get_activity(
    body: GetActivityRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> GetActivityResponse:
    """Return a mood-boosting activity not used recently and record the use (US6 / FR-034).

    The catalog is code (``activities_catalog``); selection is the pure least-recently-used
    policy over the contact's ``activity_history`` (never repeats within 30 days / the last 3,
    falling back to the least-recently-used when exhausted — SC-009). Recording the use
    AFTER selection ensures the just-offered activity is excluded next time.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    history = await activity_history_repo.list_recent(db, contact_id=contact_id)
    activity = activities_catalog.select_activity(body.kind, history, now=datetime.now(UTC))
    await activity_history_repo.record_use(
        db, contact_id=contact_id, activity_key=activity.key, call_id=call.id
    )
    await db.commit()
    # activity_key is a non-PHI catalog id (safe to log), like a tool name.
    logger.bind(call_id=str(call.id), activity=activity.key).info("Offered mood-boosting activity")
    return GetActivityResponse(
        activity_key=activity.key, title=activity.title, script=activity.script
    )


@router.post("/register_opt_out", response_model=OptOutRecordedResponse)
@track_tool("register_opt_out")
async def register_opt_out(
    body: RegisterOptOutRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> OptOutRecordedResponse:
    """Honor a spoken opt-out (US7 / FR-037): DNC the number, ack the contact, alert ops.

    Adds the contact's number to the do-not-call list so no further outbound is placed
    (SC-010), enqueues a one-time PHI-free acknowledgement to the contact, and raises a
    routine ``operator_alert`` flag so a human understands why calls stopped (FR-039).
    Idempotent within a call: the ack dedupes on the call and the operator flag is
    ensure-once.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None or not contact.phone_e164:
        raise HTTPException(status_code=409, detail="contact record has no phone number")
    # Serialize against a concurrent outbound enqueue so a dial cannot slip in between the
    # opt-out and the DNC add (mirrors the call-enqueue gate's advisory lock on the phone).
    await dnc_repo.lock_phone(db, contact.phone_e164)
    await dnc_repo.add_entry(db, contact.phone_e164, "contact opt-out during call (US7 / FR-037)")
    await notifications.enqueue_opt_out_ack(
        db, contact_id=contact_id, to_number=contact.phone_e164, dedupe_key=f"opt_out:{call.id}"
    )
    flag = await follow_up_flags_repo.ensure_opt_out_flag(
        db, call_id=call.id, contact_id=contact_id
    )
    await db.commit()
    # Bind only call_id (matches the other tools); the contact's number is PHI.
    logger.bind(call_id=str(call.id)).info("Recorded contact opt-out (DNC + ack + operator flag)")
    return OptOutRecordedResponse(status="opted_out", flag_id=flag.id)


@router.post("/send_info_sms", response_model=SmsQueuedResponse)
@track_tool("send_info_sms")
async def send_info_sms(
    body: SendInfoSmsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> SmsQueuedResponse:
    """Text the contact a PHI-free list of helpful emergency/helpline numbers (US7 / FR-041).

    The body is built server-side from ``emergency_resources`` (no operator template, no
    LLM free text), so it carries no clinical content and the numbers never drift. Shares
    the per-call SMS budget and is delivered post-call by the same outbox path as send_sms.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=409, detail="contact record not found")
    if not contact.phone_e164:
        raise HTTPException(status_code=409, detail="contact has no phone number")
    if await sms_repo.count_for_call(db, call.id) >= MAX_SMS_PER_CALL:
        raise HTTPException(status_code=409, detail="per-call SMS limit reached")
    row = await sms_repo.create_sms_message(
        db,
        call_id=call.id,
        contact_id=contact_id,
        to_number=contact.phone_e164,
        template_key="info_resources",
        body=emergency_resources.informational_sms_body(),
    )
    await db.commit()
    # Does NOT send synchronously: flush_pending_sms delivers post-call (design §6.3).
    logger.bind(call_id=str(call.id)).info("Queued send_info_sms")
    return SmsQueuedResponse(id=row.id, status=row.status)


@router.post("/retrieve_kb_context", response_model=RetrieveKbContextResponse)
@track_tool("retrieve_kb_context")
async def retrieve_kb_context(
    body: RetrieveKbContextRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> RetrieveKbContextResponse:
    """Voice-RAG (Phase 5c): retrieve KB context for the worker's current turn.

    Server-authoritative: org is bound by RLS (via the call, fail-closed) and kb_ids are
    re-derived from the resolved profile — the worker sends only {call_id, query}. Read-only
    and best-effort: any retrieval failure degrades to empty context (never 500s the call).
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact = (
        await contacts_repo.get_contact(db, call.contact_id)
        if call.contact_id is not None
        else None
    )
    resolved = await profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact.agent_profile_id if contact is not None else None,
        direction=call.direction.value,
    )
    cfg = resolved.config if resolved is not None else DEFAULT_AGENT_CONFIG
    kb_ids = cfg.llm.knowledge_base_ids or []
    try:
        retrieved = await retrieve_context(
            db,
            settings,
            kb_ids=kb_ids,
            query=body.query,
            enabled=settings.kb_retrieval_voice_enabled,
        )
    except Exception as exc:  # best-effort: retrieval NEVER breaks a call
        logger.bind(err=type(exc).__name__, kb_count=len(kb_ids)).warning(
            "voice kb retrieval failed; returning empty context"
        )
        retrieved = RetrievedContext("", 0)
    return RetrieveKbContextResponse(context=retrieved.text, hit_count=retrieved.hit_count)


@router.post("/set_spanish_callback", response_model=SpanishCallbackScheduledResponse)
@track_tool("set_spanish_callback")
async def set_spanish_callback(
    body: SetSpanishCallbackRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> SpanishCallbackScheduledResponse:
    """Record a Spanish-language preference and schedule a Spanish callback (US8 / FR-040).

    Per the spec assumption Clara does not switch languages mid-call; she promises a
    Spanish callback instead. So this records ``meta['language']='es'`` on the contact and
    creates a callback request flagged for Spanish, carrying the configured
    ``SPANISH_PROFILE_ID`` as its profile_override (or, when unset, leaving it for an
    operator). ``requested_at`` is now, so the dialer schedules it at the next allowed time.
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact_id = _require_contact(call)
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None or not contact.phone_e164:
        raise HTTPException(status_code=409, detail="contact record has no phone number")
    # Reassign (not in-place mutate) so SQLAlchemy flags the JSONB column dirty.
    contact.meta = {**contact.meta, "language": "es"}
    profile_override = (
        uuid.UUID(settings.spanish_profile_id) if settings.spanish_profile_id else None
    )
    row = await callback_requests_repo.create_callback_request(
        db,
        call_id=call.id,
        contact_id=contact_id,
        requested_time_text="Spanish-language callback (FR-040)",
        requested_at=datetime.now(UTC),
        notes=None,
        profile_override=profile_override,
    )
    await db.commit()
    # Reuse the callback metric (bounded label-free counter); no PHI in logs.
    CALLBACK_REQUESTS_TOTAL.inc()
    logger.bind(call_id=str(call.id)).info("Scheduled Spanish callback + recorded language")
    return SpanishCallbackScheduledResponse(status="scheduled", callback_id=row.id)
