import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    Time,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from usan_api.db.base import AdminRole, Base, CallDirection, CallStatus, ProfileStatus


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Store PG enum values (e.g. 'outbound'), not Python member names."""
    return [member.value for member in enum_cls]


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    external_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_voice: Mapped[str | None] = mapped_column(Text)
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    # SQLAlchemy reserves the ``metadata`` attribute on Declarative classes, so
    # the Python attribute is ``meta`` while the column stays ``metadata``.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DNCEntry(Base):
    __tablename__ = "dnc_list"

    phone_e164: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    direction: Mapped[CallDirection] = mapped_column(
        SAEnum(
            CallDirection,
            name="call_direction",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
    )
    status: Mapped[CallStatus] = mapped_column(
        SAEnum(
            CallStatus,
            name="call_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=CallStatus.QUEUED.value,
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    livekit_room: Mapped[str | None] = mapped_column(Text)
    sip_call_id: Mapped[str | None] = mapped_column(Text)
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    parent_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id")
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    end_reason: Mapped[str | None] = mapped_column(Text)
    recording_uri: Mapped[str | None] = mapped_column(Text)
    egress_id: Mapped[str | None] = mapped_column(Text)
    recording_status: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(Text)
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WellnessLog(Base):
    __tablename__ = "wellness_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    mood: Mapped[int | None] = mapped_column(SmallInteger)
    pain_level: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MedicationLog(Base):
    __tablename__ = "medication_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    medication_name: Mapped[str] = mapped_column(Text, nullable=False)
    taken: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reported_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MedicationReminder(Base):
    """A pending re-ask for a medication reported NOT taken (0020, US3).

    State machine: not-taken → ``pending`` (attempt_count=0); each repeated not-taken
    increments; confirmation → ``cleared``; reaching the re-ask cap → ``capped`` plus a
    routine ``follow_up_flags`` row. Only ``pending`` rows are surfaced as the
    ``pending_med_reasks`` builtin; a partial unique index keeps one pending row per
    ``(contact_id, medication_name)``.
    """

    __tablename__ = "medication_reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    medication_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    cleared_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonalFact(Base):
    """A durable, categorized fact about an contact (0021, US4).

    Captured by the ``record_personal_fact`` tool (``source='contact_stated'``) or the
    post-call summarizer (``source='extracted'``); operators may also seed them
    (``source='operator'``). Only ``active`` rows feed the ``personal_facts`` /
    ``important_dates`` built-ins; a superseded fact is set ``active=false`` rather than
    deleted. ``phi`` defaults true (Constitution II): a fact is protected unless proven
    otherwise. ``structured`` carries optional machine-readable detail (e.g. an
    important_date's ``{"date": "2026-07-04", "label": "birthday"}``).
    """

    __tablename__ = "personal_facts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'contact_stated'")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    phi: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationSummary(Base):
    """A per-call carry-forward recap (0021, US4).

    One row per completed call (``call_id`` is unique → the summarization trigger is
    idempotent). ``summary`` and ``open_plans`` feed the ``last_call_summary`` /
    ``open_plans`` built-ins on the contact's next call. Vertex-generated; the recap text
    is PHI and stays on BAA infra (Postgres). ``model_version`` records the summarizing
    model for audit.
    """

    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calls.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    open_plans: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    model_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WellbeingSurveyResult(Base):
    """A structured monthly wellbeing survey outcome (0023, US6).

    One row per contact per calendar month — a unique ``(contact_id, period_month)`` (migration
    -owned) enforces once-per-month (FR-032 / SC-008), so ``record_survey`` is idempotent.
    ``period_month`` is the first-of-month anchor in the contact's local month. The three
    scores are 1-5 ratings (nullable: the contact may answer only some); ``raw`` carries any
    extra structured detail. PHI — stays on BAA infra. ``call_id`` is SET NULL on call
    delete so a retention purge of a call keeps the aggregate but drops the back-link.
    """

    __tablename__ = "wellbeing_survey_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    loneliness: Mapped[int | None] = mapped_column(SmallInteger)
    mood: Mapped[int | None] = mapped_column(SmallInteger)
    satisfaction: Mapped[int | None] = mapped_column(SmallInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActivityHistory(Base):
    """Per-contact record of mood-boosting activities used (0023, US6).

    The activity catalog itself is CODE (``activities_catalog.py``); this table only tracks
    *which* activity (``activity_key``) was used *when* per contact, so ``get_activity`` can
    pick a non-recently-used one (FR-034 / SC-009). Indexed ``(contact_id, used_at desc)``
    (migration-owned) for the least-recently-used scan. ``call_id`` is SET NULL on call
    delete so a retention purge keeps the recency signal but drops the back-link.
    """

    __tablename__ = "activity_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    activity_key: Mapped[str] = mapped_column(Text, nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TurnMetrics(Base):
    __tablename__ = "turn_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    eou_delay_ms: Mapped[int | None] = mapped_column(Integer)
    transcription_delay_ms: Mapped[int | None] = mapped_column(Integer)
    stt_duration_ms: Mapped[int | None] = mapped_column(Integer)
    llm_ttft_ms: Mapped[int | None] = mapped_column(Integer)
    tts_ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    llm_completion_tokens: Mapped[int | None] = mapped_column(Integer)
    tts_characters: Mapped[int | None] = mapped_column(Integer)
    response_latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallMetrics(Base):
    __tablename__ = "call_metrics"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    llm_prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    llm_completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    llm_total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tts_characters: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stt_audio_seconds: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("0")
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    cost_telephony_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_llm_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_stt_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_tts_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_storage_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_total_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    pricing_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProfileStatus] = mapped_column(
        SAEnum(
            ProfileStatus,
            name="profile_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=ProfileStatus.ACTIVE.value,
    )
    draft_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The live version number (joins agent_profile_versions on (id, version));
    # NULL means the profile has never been published.
    published_version: Mapped[int | None] = mapped_column(Integer)
    # Optimistic-concurrency token (migration 0016). Monotonic; bumped by every
    # row-mutating path (update_draft / publish / rollback). The PUT /draft body
    # carries the loaded value as expected_revision; a guarded UPDATE that matches
    # 0 rows means the draft changed since it was loaded -> 409 (FR-032 / SC-011).
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_default_outbound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_default_inbound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentProfileVersion(Base):
    __tablename__ = "agent_profile_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    note: Mapped[str | None] = mapped_column(Text)
    published_by: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminUser(Base):
    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(
            AdminRole,
            name="admin_role",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=AdminRole.ADMIN.value,
    )
    added_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminAuditLog(Base):
    """Append-only audit trail for admin/operator mutations.

    ``actor_email`` is normally the SSO-authenticated admin's email. The
    outbound-webhook operator API (routers/webhook_endpoints.py) deliberately
    breaks that admin-session-identity assumption with the sentinel
    ``"operator-api-key"``: the operator-key plane carries no per-user
    identity, and durable DB audit is wanted for egress configuration changes
    (outbound-webhooks spec §4). ``detail`` carries ids and changed field
    names only — never secrets, never URLs.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_email: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FollowUpFlag(Base):
    __tablename__ = "follow_up_flags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete on contacts: a follow-up flag's clinical context must outlive an
    # contact row removal (it stays referenced for audit), unlike call-scoped data.
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_by: Mapped[str | None] = mapped_column(Text)  # admin actor email
    # Crisis escalation columns (0018, US1). Populated only on crisis flags (NULL on
    # ordinary follow-up flags). detection_source becomes 'both' when the LLM and the
    # deterministic safety net independently flag the same (call_id, crisis_category).
    crisis_category: Mapped[str | None] = mapped_column(Text)
    detection_source: Mapped[str | None] = mapped_column(Text)
    resource_offered: Mapped[str | None] = mapped_column(Text)
    family_notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    requested_time_text: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_by: Mapped[str | None] = mapped_column(Text)  # admin actor email
    # Set by the callback dialer (US8): the outbound Call materialized for this callback,
    # and the agent profile a Spanish callback should dial with (SET NULL on delete).
    dispatched_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SmsMessage(Base):
    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Nullable since 0017: a non-call notification (family alert/report, opt-out ack)
    # has no owning call. In-call texts still set it (FK CASCADE drops them with the call).
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    to_number: Mapped[str] = mapped_column(Text, nullable=False)
    # Discriminator (0017): 'in_call' = an LLM-selected template SMS; the others are
    # system-template notifications flushed by the notification outbox.
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_call'"))
    # NULL for system-template notifications (0017): they carry no per-profile key.
    template_key: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    # Idempotency key for family notifications (e.g. 'crisis:{flag_id}'); unique-where-
    # not-null (0017, NULLs distinct in PG). NULL for in-call rows.
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    telnyx_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CallSchedule(Base):
    __tablename__ = "call_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE: a schedule is meaningless without its contact; calls.contact_id SET NULLs
    # independently, so call history survives (spec §3.1). One schedule per
    # (contact, slot) — the composite UNIQUE(contact_id, slot) is owned by migration 0022
    # (this module keeps constraints in migrations, like the ck_* CHECKs).
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # US5: morning|evening daily-call slot; an contact may have one schedule per slot.
    # The morning|evening CHECK lives in migration 0022.
    slot: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'morning'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Contact-local wall clock; the contact's timezone column is the single source of truth.
    window_start_local: Mapped[time] = mapped_column(Time, nullable=False)
    window_end_local: Mapped[time] = mapped_column(Time, nullable=False)
    # Bitmask, bit 0=Mon … bit 6=Sun; 127 = all seven days.
    days_of_week: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("127")
    )
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    # Computed in Python (zoneinfo); never SQL tz math.
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_materialized_date: Mapped[date | None] = mapped_column(Date)
    last_result: Mapped[str | None] = mapped_column(Text)
    last_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CallBatch(Base):
    __tablename__ = "call_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Operator label; PHI-free by convention (spec §8).
    name: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    # sha256 of the canonical payload (spec §4.2 replay guard).
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scheduled'"))
    trigger_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional per-contact-local dial window (both NULL or both set, CHECK-enforced).
    window_start_local: Mapped[time | None] = mapped_column(Time)
    window_end_local: Mapped[time | None] = mapped_column(Time)
    days_of_week: Mapped[int | None] = mapped_column(SmallInteger)
    # Materialization throttle, NOT a dial cap (spec §5.2).
    max_concurrency: Mapped[int | None] = mapped_column(SmallInteger)
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Also stamped on drained cancelled batches (bounded poller working set).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CallBatchTarget(Base):
    __tablename__ = "call_batch_targets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_batches.id", ondelete="CASCADE"), nullable=False
    )
    # Position in the submitted array (UNIQUE (batch_id, target_index) in 0012).
    target_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # SET NULL (not CASCADE): a deleted contact must not silently shrink the batch;
    # the poller marks the orphan target skipped/contact_deleted instead (spec §3.3).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    # contact_deleted | invalid_timezone | key_conflict | daily_cap
    skip_reason: Mapped[str | None] = mapped_column(Text)
    # Root attempt; SET NULL keeps the target's audit row if the call is purged.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    # Denormalized terminal CallStatus of the LAST attempt (spec §6.2).
    final_status: Mapped[str | None] = mapped_column(Text)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # 64 hex chars (32 random bytes), server-generated, returned once at create,
    # NEVER logged (spec §4/§8.3).
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    # Subscription list, CHECK-constrained to the closed event enum in 0014
    # ('ping' is deliberately not subscribable).
    events: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # NULL for operator disables (enabled=false), 'circuit_breaker' for auto-disables —
    # the two states stay distinguishable (spec §3.3).
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    response_code: Mapped[int | None] = mapped_column(Integer)
    # Exception TYPE NAME only, never str(exc) (PHI-adjacent rule, spec §5.3/§8.2).
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CustomVariable(Base):
    """Operator-declared prompt variable (catalog tier ``custom``, migration 0015).

    Definitions are documentation/UX only — values arrive per call via
    ``Call.dynamic_vars``, never through this table. ``name`` is immutable after
    create (a rename would silently orphan ``{{tokens}}`` already saved in
    templates; delete + recreate instead). Collisions with the 10 frozen builtin
    names are enforced in the Pydantic layer — authority stays in code; the DB
    enforces only slug shape + uniqueness.
    """

    __tablename__ = "custom_variables"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    example: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    phi: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FamilyContact(Base):
    """A person linked to an contact who can send tasks and receive alerts/reports (0019, US2).

    No ondelete on contact_id (a contact's context outlives an contact row change, like
    follow_up_flags). phone_e164 is the inbound-SMS routing key and is NOT globally
    unique — one number may relate to more than one contact.
    """

    __tablename__ = "family_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    relationship: Mapped[str | None] = mapped_column(Text)
    # Which alert kinds this contact receives (e.g. {"crisis": true, "missed": false}).
    # Missing/true => opted in (fail-open for life-safety alerts).
    alert_prefs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FamilyTask(Base):
    """A short instruction from a family contact to convey to the contact then close (0019, US2).

    State machine: open → delivered → closed; open → needs_review (operator) → open/closed.
    Only ``open`` tasks that are not ``needs_safety_review`` are injected as the
    ``open_family_tasks`` builtin. Audit fields mirror follow_up_flags.
    """

    __tablename__ = "family_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    # Null when an operator entered the task directly (no inbound contact source).
    family_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_contacts.id")
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    needs_safety_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    delivered_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    # Telnyx inbound message id — idempotency key for the webhook intake. NULL for
    # operator-entered tasks (unique-where-not-null: many NULLs allowed in PG).
    inbound_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_by: Mapped[str | None] = mapped_column(Text)


class FamilyReport(Base):
    """A generated monthly per-contact status-and-trends report (0025, US8).

    One row per contact per calendar month — unique ``(contact_id, period_month)`` makes the
    monthly job idempotent (FR-012 / SC-012). ``metrics`` (mood/adherence/survey aggregates)
    and ``narrative`` are PHI and stay on BAA infra; the PHI-minimized family SMS that is
    actually sent is linked via ``sms_message_id``. ``status`` is 'sent' (a family contact
    was notified) or 'no_contact' (no family registered — operators follow up, FR-013).
    """

    __tablename__ = "family_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    calls_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    narrative: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'sent'"))
    sms_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sms_messages.id", ondelete="SET NULL")
    )
    model_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantScoped:
    """Mixin adding the tenant FK. Applied to every tenant-owned model in Tasks 4-5.

    The column is added to the DB by migrations 0031/0032; this mixin keeps the ORM
    mapping in sync. organization_id is filled by a DB column DEFAULT sourced from the
    tenant context on INSERT (see the migrations' SET DEFAULT) — so repositories never
    set it and existing insert code is unchanged — and RLS WITH CHECK rejects any
    cross-org mismatch. The server_default below mirrors that DDL so SQLAlchemy omits
    the column from INSERTs and reads it back via RETURNING.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
        index=True,
        server_default=text(
            "COALESCE(current_setting('app.current_org', true)::uuid,"
            " (SELECT id FROM organizations WHERE slug = 'usan'))"
        ),
    )
