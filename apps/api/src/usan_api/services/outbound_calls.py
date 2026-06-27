"""Shared outbound-call enqueue/dispatch core.

Plane-agnostic: takes whatever AsyncSession the caller holds — the operator
``POST /v1/calls`` (get_db) and the admin ``POST /v1/admin/calls`` (get_tenant_db)
both delegate here, so DNC/liveness/dispatch/retry behavior is identical.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import dialer, livekit_dispatch
from usan_api.builtin_vars import build_memory_params, resolve_builtin_vars
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Contact
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import conversation_summaries as conversation_summaries_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.repositories import medication_reminders as medication_reminders_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import survey_results as survey_results_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest
from usan_api.settings import Settings

OVERRIDE_ERROR = "profile_override must reference an active profile with a published version"


async def require_live_override(db: AsyncSession, profile_id: uuid.UUID) -> None:
    """422 unless the override would actually take effect (ACTIVE + published)."""
    if not await agent_profiles_repo.is_live_profile(db, profile_id, channel="voice"):
        raise HTTPException(status_code=422, detail=OVERRIDE_ERROR)


async def enforce_dialing_gates(db: AsyncSession, settings: Settings) -> None:
    """Apply the emergency-stop and concurrency umbrella to every real-time dial.

    These guards previously lived ONLY in the retry poller (``retry_orchestrator``),
    so the operator ``POST /v1/calls``, admin call-now, and compat create-phone-call
    paths — all of which call ``create_and_dispatch`` directly — bypassed both the
    ``AUTONOMOUS_DIALING_PAUSED`` emergency stop and the ``max_concurrent_calls`` cap.
    Enforcing them here closes that gap for all three planes at once.

    The pause flag is always honored (it is the state-preserving emergency stop). The
    concurrency cap is honored only when ``concurrency_gate_enabled`` and uses the same
    in-flight count / free-slot arithmetic as the poller, so real-time and poller-driven
    dials draw from one shared budget instead of two independent ones.
    """
    if settings.autonomous_dialing_paused:
        raise HTTPException(status_code=503, detail="outbound dialing is paused")
    if settings.concurrency_gate_enabled:
        in_flight = await calls_repo.count_in_flight(
            db,
            now=datetime.now(UTC),
            max_age_s=settings.outbound_max_call_duration_s + 120,
        )
        free = settings.max_concurrent_calls - settings.reserved_concurrency - in_flight
        if free <= 0:
            raise HTTPException(status_code=503, detail="outbound calling is at capacity")


async def create_and_dispatch(
    db: AsyncSession,
    *,
    body: CreateCallRequest,
    contact: Contact,
    settings: Settings,
) -> CallResponse:
    """Persist a queued call, dispatch the agent, schedule the background dial.

    Reject up front when the emergency stop is engaged or the concurrency budget is
    exhausted, so no call row is created and no telephony spend is incurred while paused
    or at capacity (spec §5.4). All three real-time origination planes share this core.
    """
    await enforce_dialing_gates(db, settings)
    room = f"usan-outbound-{uuid.uuid4()}"
    call = await calls_repo.create_call(
        db,
        contact_id=contact.id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        idempotency_key=body.idempotency_key,
        livekit_room=room,
        dynamic_vars=body.dynamic_vars,
        profile_override=body.profile_override,
    )
    await db.commit()

    last = await wellness_repo.get_latest_for_contact(db, contact.id)
    open_tasks = await family_tasks_repo.list_open_family_tasks(db, contact_id=contact.id)
    pending_meds = await medication_reminders_repo.list_pending(db, contact_id=contact.id)
    facts = await personal_facts_repo.list_active(db, contact_id=contact.id)
    summary = await conversation_summaries_repo.get_latest(db, contact_id=contact.id)
    memory = build_memory_params(
        facts, summary, timezone=contact.timezone or "", now=datetime.now(UTC)
    )
    period_month = survey_results_repo.month_anchor(contact.timezone or "", datetime.now(UTC))
    survey_due = not await survey_results_repo.exists_for_month(
        db, contact_id=contact.id, period_month=period_month
    )
    resolved_vars, timezone = resolve_builtin_vars(
        contact,
        last,
        direction="outbound",
        open_family_tasks=[t.message for t in open_tasks],
        pending_med_reasks=[r.medication_name for r in pending_meds],
        survey_due=survey_due,
        **memory,
    )
    try:
        await livekit_dispatch.dispatch_agent(
            call, settings=settings, resolved_vars=resolved_vars, timezone=timezone
        )
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(db, call.id, CallStatus.FAILED, error={"reason": str(exc)})
        await db.commit()
        raise HTTPException(status_code=503, detail="outbound calling is not available") from exc
    except Exception as exc:
        await calls_repo.set_status(
            db,
            call.id,
            CallStatus.FAILED,
            error={"reason": "dispatch_error", "exc_type": type(exc).__name__},
        )
        await calls_repo.schedule_retry(db, call.id)
        await db.commit()
        logger.bind(call_id=str(call.id), err=type(exc).__name__).error("Agent dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    dialing = await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    dialer.schedule_dial(call.id, settings)
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched; dialing")
    return CallResponse.from_model(dialing or call)
