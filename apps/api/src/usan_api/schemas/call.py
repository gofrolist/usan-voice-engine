import json
import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from usan_api.db.models import Call, Transcript
from usan_api.schemas._validators import reject_nested_dynamic_vars

# Inbound caller-ID guard: reject ONLY genuine log-forging / injection bytes — control
# characters (newlines, NUL, DEL). Everything printable is allowed on purpose: a SIP
# From-header caller-ID can legitimately carry display-name / parameter punctuation
# (``<``, ``>``, ``"``, ``;tag=``, ``=``) that to_e164() then strips to recover the
# number (calls.py), so rejecting it would silently de-personalize a known caller. The
# pre-existing max_length=32 bounds length; SQL/template metacharacters are inert here
# (the lookup is a parameterized query and the value is normalized before any use).
_INBOUND_CALLER_ID_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")

# Cap the serialized dynamic_vars payload: it is persisted to JSONB and relayed
# verbatim as LiveKit agent-dispatch metadata, so it must stay small.
MAX_DYNAMIC_VARS_BYTES = 8192

# The materializer owns these prefixes (spec §2.2 invariant 3): a squatted key could
# otherwise suppress or substitute a wellness call (§5.3 step 5 verifies ownership).
RESERVED_KEY_PREFIXES = ("sched:", "batch:")


class CreateCallRequest(BaseModel):
    contact_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)
    # Validated live (ACTIVE + published) on the create path only; part of the
    # idempotency payload contract — replays must match it exactly (spec §3.1).
    profile_override: uuid.UUID | None = None

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        reject_nested_dynamic_vars(v)
        if len(json.dumps(v)) > MAX_DYNAMIC_VARS_BYTES:
            raise ValueError(f"dynamic_vars must serialize to <= {MAX_DYNAMIC_VARS_BYTES} bytes")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def _reject_reserved_namespace(cls, v: str) -> str:
        if v.startswith(RESERVED_KEY_PREFIXES):
            raise ValueError("idempotency_key prefixes 'sched:'/'batch:' are reserved")
        return v


class CallOutcomeRequest(BaseModel):
    outcome: Literal["voicemail_left"]


class CallOrigin(BaseModel):
    source: Literal["schedule", "batch"]
    id: uuid.UUID
    ordinal: str | int  # local_date for schedules, target_index for batches


def parse_origin(idempotency_key: str | None) -> CallOrigin | None:
    """Derived, read-only provenance from the materializer's reserved key namespace
    (spec §4.3). Malformed values return None — never raise on stored data."""
    if idempotency_key is None:
        return None
    parts = idempotency_key.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, raw_id, raw_ordinal = parts
    try:
        owner_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    if prefix == "sched" and raw_ordinal:
        return CallOrigin(source="schedule", id=owner_id, ordinal=raw_ordinal)
    if prefix == "batch":
        try:
            return CallOrigin(source="batch", id=owner_id, ordinal=int(raw_ordinal))
        except ValueError:
            return None
    return None


class TranscriptSegment(BaseModel):
    role: str
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime | None = None

    @classmethod
    def from_model(cls, t: Transcript) -> TranscriptSegment:
        return cls(
            role=t.role,
            content=t.content,
            tool_name=t.tool_name,
            tool_args=t.tool_args,
            started_at=t.started_at,
            ended_at=t.ended_at,
        )


class CallResponse(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID | None
    direction: str
    status: str
    idempotency_key: str | None
    livekit_room: str | None
    attempt: int
    recording_uri: str | None
    egress_id: str | None
    recording_status: str | None
    presigned_recording_url: str | None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    created_at: datetime
    # Derived provenance (spec §4.3): parsed from this call's own reserved-namespace
    # idempotency_key; None for operator keys and retry children (chain walk applies).
    origin: CallOrigin | None = None
    # Echo for the operator system and day-2 triage (spec §3.1); surfacing it as
    # an A2 calls-console column is deferred (Open Q6).
    profile_override: uuid.UUID | None

    @classmethod
    def from_model(
        cls,
        call: Call,
        *,
        presigned_recording_url: str | None = None,
        transcript: Sequence[Transcript] = (),
    ) -> CallResponse:
        return cls(
            id=call.id,
            contact_id=call.contact_id,
            direction=call.direction.value,
            status=call.status.value,
            idempotency_key=call.idempotency_key,
            livekit_room=call.livekit_room,
            attempt=call.attempt,
            recording_uri=call.recording_uri,
            egress_id=call.egress_id,
            recording_status=call.recording_status,
            presigned_recording_url=presigned_recording_url,
            transcript=[TranscriptSegment.from_model(t) for t in transcript],
            created_at=call.created_at,
            origin=parse_origin(call.idempotency_key),
            profile_override=call.profile_override,
        )


class InboundCallRequest(BaseModel):
    phone_e164: str | None = Field(default=None, max_length=32)
    livekit_room: str = Field(min_length=1, max_length=255)
    sip_call_id: str | None = Field(default=None, max_length=255)
    # Surface 2A: the dialed DID (callee), read from the SIP participant by the worker. Sent to
    # the client's inbound-call-router as ``call_inbound.to_number``. Captured-but-unused when the
    # router is off. Same lenient caller-ID charset guard as ``phone_e164``.
    to_number: str | None = Field(default=None, max_length=32)

    @field_validator("phone_e164", "to_number")
    @classmethod
    def _caller_id_charset(cls, v: str | None) -> str | None:
        # Lenient guard (NOT strict E.164): blank => None; otherwise the value must be a
        # plausible phone/SIP caller-ID. to_e164() still normalizes it downstream.
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        if _INBOUND_CALLER_ID_UNSAFE.search(s):
            raise ValueError("caller-ID contains control characters")
        return s


class InboundCallResponse(BaseModel):
    call_id: uuid.UUID
    contact_known: bool
    dynamic_vars: dict[str, Any]
    # Phase 2 (contract C): the 8 server-resolved data built-ins + the contact's IANA
    # timezone, passed to the agent out-of-band. Additive with defaults so older
    # agent builds that ignore them keep working.
    resolved_vars: dict[str, str] = Field(default_factory=dict)
    timezone: str = ""
    # Surface 2A: True when the inbound-call-router returned an override agent we resolved to a
    # published voice profile (stored as the call's profile_override). Signals the worker to run
    # the personalized inbound agent with the override config even for an unknown caller. Additive
    # default False so older worker builds keep the contact-lookup path.
    override_applied: bool = False
