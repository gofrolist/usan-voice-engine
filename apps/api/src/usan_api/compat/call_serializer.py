"""Assemble the full RetellAI Call object from native rows (feature 003, data-model §3).

PHI: the recording URL + transcript are PHI returned ONLY to the authenticated CRM over its
own org-scoped key (its own data) via the synchronous API — the allow-list gate applies to
webhook PUSH delivery, not these pull responses. ``recording_url`` is signed by the existing
keyless presigner, which emits a PHI-audit line (never the URL).
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import phi_audit, recording_urls
from usan_api.compat import ids, status_map
from usan_api.compat.schemas.calls import (
    CallAnalysis,
    CallCost,
    CompatCall,
    TranscriptUtterance,
)
from usan_api.compat.serialization import duration_ms, to_ms, unpack_dynamic_vars
from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call, CallMetrics, Transcript
from usan_api.livekit_dispatch import mint_browser_token
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import conversation_summaries as conversation_summaries_repo
from usan_api.repositories import metrics as metrics_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.settings import Settings


def _retell_role(native_role: str) -> str:
    # Native roles are user/agent/assistant; RetellAI uses user/agent.
    return "user" if native_role == "user" else "agent"


def _segment_line(seg: Transcript, *, with_tools: bool) -> str:
    role = "User" if seg.role == "user" else "Agent"
    if with_tools and seg.tool_name:
        return f"{role} [tool:{seg.tool_name}]: {seg.content}"
    return f"{role}: {seg.content}"


async def _resolve_agent(db: AsyncSession, call: Call) -> tuple[str | None, str | None, int | None]:
    """(agent_id, agent_name, agent_version) for the profile that handled the call: the
    override if any, else the direction default. (None, None, None) when none is resolvable."""
    direction: Literal["inbound", "outbound"] = (
        "inbound" if call.direction is CallDirection.INBOUND else "outbound"
    )
    profile = None
    if call.profile_override is not None:
        profile = await agent_profiles_repo.get_profile(db, call.profile_override)
    if profile is None:
        profile = await agent_profiles_repo.get_default_profile(db, direction)
    if profile is None:
        return None, None, None
    return ids.encode_agent_id(profile.id), profile.name, profile.published_version


async def _build_analysis(db: AsyncSession, call: Call) -> CallAnalysis | None:
    summary = await conversation_summaries_repo.get_for_call(db, call.id)
    if summary is None and not status_map.is_terminal(call.status):
        return None
    custom = {"open_plans": summary.open_plans} if summary is not None else None
    return CallAnalysis(
        call_summary=summary.summary if summary is not None else None,
        in_voicemail=call.status is CallStatus.VOICEMAIL_LEFT,
        call_successful=status_map.call_successful(call.status),
        custom_analysis_data=custom,
    )


def _build_cost(m: CallMetrics | None) -> CallCost | None:
    if m is None:
        return None
    return CallCost(
        combined_cost=float(m.cost_total_usd),
        total_duration_seconds=m.duration_seconds,
        product_costs=[
            {"product": "telephony", "cost": float(m.cost_telephony_usd)},
            {"product": "llm", "cost": float(m.cost_llm_usd)},
            {"product": "stt", "cost": float(m.cost_stt_usd)},
            {"product": "tts", "cost": float(m.cost_tts_usd)},
            {"product": "storage", "cost": float(m.cost_storage_usd)},
        ],
        pricing_version=m.pricing_version,
    )


def _token_usage(m: CallMetrics | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "prompt_tokens": m.llm_prompt_tokens,
        "completion_tokens": m.llm_completion_tokens,
        "total_tokens": m.llm_total_tokens,
    }


async def serialize_call(
    db: AsyncSession,
    call: Call,
    settings: Settings,
    *,
    client_host: str,
    include_transcript: bool = True,
    include_recording: bool = True,
) -> CompatCall:
    """Build the RetellAI Call object. ``include_*`` are False on the list path (lighter, and
    avoids one IAM signing call + one transcript query per row); full fidelity via get-call."""
    contact = (
        await contacts_repo.get_contact(db, call.contact_id)
        if call.contact_id is not None
        else None
    )
    agent_id, agent_name, agent_version = await _resolve_agent(db, call)
    dynamic_variables, metadata = unpack_dynamic_vars(call.dynamic_vars)

    transcript_str: str | None = None
    transcript_object: list[TranscriptUtterance] = []
    if include_transcript:
        segments = await transcripts_repo.list_for_call(db, call.id)
        transcript_object = [
            TranscriptUtterance(role=_retell_role(s.role), content=s.content)
            for s in segments
            if s.tool_name is None
        ]
        if segments:
            # PHI access audit to the locked sink (ids/host only), mirroring native get-call.
            phi_audit.log_transcript_accessed(
                call_id=call.id, client=client_host, segments=len(segments)
            )
            transcript_str = "\n".join(
                _segment_line(s, with_tools=False) for s in segments if s.tool_name is None
            )

    recording_url: str | None = None
    if include_recording:
        recording_url = await recording_urls.presigned_recording_url(
            call, settings, client_host=client_host
        )

    # Pre-answer statuses (REGISTERED/QUEUED/DIALING/RINGING) definitionally have no
    # CallMetrics row — create_metrics is only called at call-end (agent tool callback).
    # Skipping the PK lookup for those statuses is behavior-preserving: db.get returns None
    # either way, and _build_cost(None)/_token_usage(None) both return None.
    _pre_answer = {
        CallStatus.REGISTERED,
        CallStatus.QUEUED,
        CallStatus.DIALING,
        CallStatus.RINGING,
    }
    metrics = (
        None if call.status in _pre_answer else await metrics_repo.get_call_metrics(db, call.id)
    )

    is_web = call.call_type is CallType.WEB_CALL
    access_token: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    if is_web:
        # A web call's browser join token: minted on demand, scoped to this room,
        # never persisted, never logged. Phone-only fields are omitted (V2WebCallResponse).
        access_token = mint_browser_token(
            settings, room=call.livekit_room or "", identity=ids.encode_call_id(call.id)
        )
    else:
        # OUTBOUND: from = our caller id, to = the contact. INBOUND is reversed — the
        # contact is the caller and our DID is the callee.
        contact_phone = contact.phone_e164 if contact is not None else None
        if call.direction is CallDirection.INBOUND:
            from_number, to_number = contact_phone, settings.telnyx_caller_id
        else:
            from_number, to_number = settings.telnyx_caller_id, contact_phone

    return CompatCall(
        call_id=ids.encode_call_id(call.id),
        call_type="web_call" if is_web else "phone_call",
        agent_id=agent_id,
        agent_name=agent_name,
        agent_version=agent_version,
        call_status=status_map.to_call_status(call.status),
        access_token=access_token,
        from_number=from_number,
        to_number=to_number,
        direction=None if is_web else call.direction.value,
        telephony_identifier=(
            None
            if is_web
            else ({"twilio_call_sid": call.sip_call_id} if call.sip_call_id else None)
        ),
        metadata=metadata,
        retell_llm_dynamic_variables=dynamic_variables,
        start_timestamp=to_ms(call.answered_at),
        end_timestamp=to_ms(call.ended_at),
        duration_ms=duration_ms(call.duration_seconds),
        transcript=transcript_str,
        transcript_object=transcript_object,
        recording_url=recording_url,
        disconnection_reason=status_map.to_disconnection_reason(call.status),
        call_analysis=await _build_analysis(db, call),
        call_cost=_build_cost(metrics),
        llm_token_usage=_token_usage(metrics),
    )
