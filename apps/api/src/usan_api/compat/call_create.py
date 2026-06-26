"""Compat call-create service (feature 003, T022).

Number-first Contact upsert, create-time DNC + quiet-hours gating (explicit 400 — the
stakeholder decision), deterministic idempotency synthesis, then reuse of the native
dispatch core (``services.outbound_calls.create_and_dispatch``) so dial + built-in-resolution logic
never drifts from the native ``/v1/calls`` path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Response
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch, quiet_hours
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import decode_agent_id
from usan_api.compat.schemas.calls import (
    CreatePhoneCallRequest,
    CreateWebCallRequest,
    RegisterPhoneCallRequest,
)
from usan_api.compat.serialization import RESERVED_VAR_PREFIX, pack_dynamic_vars, pack_unhonored
from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call, Contact
from usan_api.phone import to_e164
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.schemas.call import CreateCallRequest
from usan_api.services import outbound_calls
from usan_api.settings import Settings


def _synth_idempotency_key(
    organization_id: uuid.UUID,
    *,
    to_number: str,
    from_number: str,
    agent: str | None,
    packed: dict[str, Any],
) -> str:
    """Deterministic key so a CRM retry never double-dials (Constitution V). RetellAI does
    not expose idempotency, so it is synthesized from the call's identity. A bare sha256 hex
    never collides with the reserved sched:/batch: namespaces.

    FROZEN (oracle): two identical-payload calls dedupe to the same call_id —
    pinned by test_duplicate_create_is_idempotent.
    """
    payload = json.dumps(
        {
            "org": str(organization_id),
            "to": to_number,
            "from": from_number,
            "agent": agent or "",
            "vars": packed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def upsert_contact_for_number(
    db: AsyncSession,
    settings: Settings,
    phone: str,
    metadata: dict[str, Any] | None,
) -> Contact:
    """Number-first Contact lazy upsert (T022) — the shim US1 and US4 share.

    Reuse an existing Contact by E.164, else create one from the CRM's metadata
    (defaults: name = the E.164 number, timezone = COMPAT_DEFAULT_TIMEZONE).

    FROZEN (oracle): metadata keys ``name`` and ``external_id`` — pinned by
    test_metadata_name_and_external_id_populate_contact (contact upsert path exercised).
    """
    contact = await contacts_repo.get_contact_by_phone(db, phone)
    if contact is not None:
        return contact
    meta = metadata or {}
    name = str(meta.get("name") or phone)
    ext = meta.get("external_id")
    return await contacts_repo.create_contact(
        db,
        name=name,
        phone_e164=phone,
        timezone=settings.compat_default_timezone,
        external_id=str(ext) if ext is not None else None,
    )


async def create_compat_call(
    db: AsyncSession,
    settings: Settings,
    body: CreatePhoneCallRequest,
    response: Response,
    *,
    organization_id: uuid.UUID,
) -> Call:
    """Gate + dispatch a RetellAI-shaped phone call; returns the created (or replayed) Call."""
    phone = to_e164(body.to_number)
    if phone is None:
        raise CompatError(422, "invalid to_number")
    profile_override = decode_agent_id(body.override_agent_id) if body.override_agent_id else None
    # Reject CRM dynamic-var keys that collide with the reserved metadata namespace
    # (otherwise they would be silently swallowed / corrupt the metadata round-trip).
    if any(
        str(k).startswith(RESERVED_VAR_PREFIX) for k in (body.retell_llm_dynamic_variables or {})
    ):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")
    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    # Key off the NORMALISED E.164 number (not the raw CRM string), so a retry that reformats
    # the number ('+1 415…' vs '+1415…') hits the same key and never double-dials (FR-012).
    key = _synth_idempotency_key(
        organization_id,
        to_number=phone,
        from_number=to_e164(body.from_number) or body.from_number,
        agent=body.override_agent_id,
        packed=packed,
    )

    # Serialize the DNC-check / create window for this number (mirrors native enqueue_call).
    await dnc_repo.lock_phone(db, phone)

    # Idempotent replay BEFORE any side effect: an identical synthesized key returns the
    # original call (no re-dial, no contact churn), matching the native ordering contract.
    existing = await calls_repo.get_by_idempotency_key(db, key)
    if existing is not None:
        return existing

    # Create-time DNC gate -> EXPLICIT 400 (stakeholder decision), before the Contact is
    # materialized; the native path instead records a DNC_BLOCKED row + 200.
    if await dnc_repo.is_blocked(db, phone):
        raise CompatError(400, "blocked_dnc")

    contact = await upsert_contact_for_number(db, settings, phone, body.metadata)

    if profile_override is not None and not await agent_profiles_repo.is_live_profile(
        db, profile_override
    ):
        raise CompatError(422, "override_agent_id must reference a published agent")

    # Oracle V2CallBase requires a non-null agent_id/agent_version on every call. When the
    # caller gives no override, a PUBLISHED default outbound profile must exist; otherwise we
    # would emit a null-agent (non-conformant) call. Refuse early with 422.
    if profile_override is None:
        default = await agent_profiles_repo.get_default_profile(db, "outbound")
        if default is None or not await agent_profiles_repo.is_live_profile(db, default.id):
            raise CompatError(422, "no published agent profile available to place the call")

    # Create-time quiet-hours gate -> EXPLICIT 400 (the native path only checks at dial time).
    now = datetime.now(UTC)
    try:
        allowed = quiet_hours.next_allowed(now, contact.timezone)
    except ValueError as exc:
        # Fail CLOSED on an unresolvable timezone. create_and_dispatch dispatches the
        # SIP call immediately (no future scheduled_at), so there is NO dial-time
        # backstop here — proceeding would place a call we cannot prove is inside the
        # contact's allowed (TCPA) hours. The native scheduled paths fail closed too.
        raise CompatError(400, "blocked_quiet_hours") from exc
    if allowed > now:
        raise CompatError(400, "blocked_quiet_hours")

    try:
        native_body = CreateCallRequest(
            contact_id=contact.id,
            idempotency_key=key,
            dynamic_vars=packed,
            profile_override=profile_override,
        )
    except ValidationError as exc:
        # e.g. dynamic_vars over the byte cap or with nested values.
        raise CompatError(422, "invalid retell_llm_dynamic_variables or metadata") from exc

    try:
        native_resp = await outbound_calls.create_and_dispatch(
            db, body=native_body, contact=contact, settings=settings
        )
    except IntegrityError as exc:
        await db.rollback()
        existing = await calls_repo.get_by_idempotency_key(db, key)
        if existing is None:
            raise CompatError(409, "idempotency_key conflict") from exc
        return existing
    call = await calls_repo.get_call(db, native_resp.id)
    if call is None:  # pragma: no cover - the row was just committed
        raise CompatError(500, "internal error")
    return call


async def register_compat_call(
    db: AsyncSession,
    body: RegisterPhoneCallRequest,
    settings: Settings,
) -> Call:
    """Create a Call row in REGISTERED status WITHOUT dialing.

    Oracle: agent_id is required and must reference a live (published) profile.
    The outbound poller (claim_due_retries) only claims QUEUED rows, so a REGISTERED
    row is structurally excluded from auto-dial.
    """
    profile_id = decode_agent_id(body.agent_id)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent_id must reference a published agent")

    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)

    # Use a placeholder contact for register calls that lack a to_number.
    phone = to_e164(body.to_number) if body.to_number else None
    if phone is not None:
        contact = await upsert_contact_for_number(db, settings, phone, body.metadata)
    else:
        # No to_number: synthesize a placeholder contact keyed on a unique sentinel.
        sentinel = f"+1000{str(uuid.uuid4().int % 10**7).zfill(7)}"
        contact = await upsert_contact_for_number(db, settings, sentinel, body.metadata)

    direction = CallDirection.INBOUND if body.direction == "inbound" else CallDirection.OUTBOUND

    call = Call(
        contact_id=contact.id,
        direction=direction,
        status=CallStatus.REGISTERED,
        scheduled_at=None,
        profile_override=profile_id,
        dynamic_vars=packed,
    )
    db.add(call)
    await db.flush()
    await db.commit()
    await db.refresh(call)
    return call


async def create_web_call(
    db: AsyncSession,
    settings: Settings,
    body: CreateWebCallRequest,
) -> Call:
    """Create + dispatch a live LiveKit web call; returns the REGISTERED web Call row.

    No contact / DNC / quiet-hours (web calls are join-link, not PSTN). The agent is
    resolved + gated exactly like register-phone-call (agent_id required + published).
    """
    profile_id = decode_agent_id(body.agent_id)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent_id must reference a published agent")
    if any(
        str(k).startswith(RESERVED_VAR_PREFIX) for k in (body.retell_llm_dynamic_variables or {})
    ):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")

    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    packed = pack_unhonored(
        packed,
        agent_override=body.agent_override,
        current_node_id=body.current_node_id,
        current_state=body.current_state,
    )
    room = f"usan-web-{uuid.uuid4().hex}"
    call = Call(
        call_type=CallType.WEB_CALL,
        status=CallStatus.REGISTERED,
        direction=CallDirection.INBOUND,  # internal placeholder; call_type is authoritative.
        # Web calls are excluded from direction-keyed internal analytics:
        # admin call list (repositories/admin_calls.py) filters to PHONE_CALL,
        # and end_call CALLS_TOTAL metric skips WEB_CALL (routers/tools.py).
        profile_override=profile_id,
        dynamic_vars=packed,
        livekit_room=room,
        contact_id=None,
    )
    db.add(call)
    await db.flush()

    # Dispatch before commit (like the native paths): on failure we roll back so no
    # orphan Call persists. The worker's connect→fetch_agent_config latency dwarfs this
    # local commit, so the row is visible well before the agent reads it.
    try:
        await livekit_dispatch.dispatch_web_agent(
            settings=settings,
            room=room,
            call_id=str(call.id),
            dynamic_vars=body.retell_llm_dynamic_variables or {},
            resolved_vars={},
            timezone=settings.compat_default_timezone,
        )
    except Exception as exc:
        # PHI/secret-safe: type name only — never str(exc), metadata, or any token.
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("web call dispatch failed")
        raise CompatError(502, "web call dispatch failed") from None

    await db.commit()
    # No db.refresh: the SET LOCAL tenant context clears at commit, so a post-commit
    # SELECT would be hidden by RLS. All fields serialize_call reads are already
    # populated in-memory (set explicitly above, or via flush() RETURNING for id +
    # organization_id) — there is no server_default field the serializer needs refreshed.
    return call
