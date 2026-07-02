import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Literal

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import CallStatus, ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion, Contact
from usan_api.quiet_hours import QUIET_END_HOUR, QUIET_START_HOUR
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ResolvedAgentConfig


class ProfileInUseError(Exception):
    """Archiving or defaulting is blocked (profile is a live default or assigned to contacts).

    The message is returned verbatim in the API 409 response body, so keep it
    user-facing and free of internal detail.
    """


class CloneSourceNotFoundError(Exception):
    """Raised when create_profile(clone_from=...) references a profile that doesn't exist."""


class StaleDraftError(Exception):
    """The draft changed since the editor loaded it (optimistic concurrency, FR-032).

    Raised by update_draft only when an ``expected_revision`` was supplied and the
    guarded UPDATE matched 0 rows while the row still exists. The router maps it to
    HTTP 409 with a generic reload-prompt detail — never any PHI or other actor's
    identity (spec §7).
    """


async def create_profile(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    actor_email: str,
    clone_from: uuid.UUID | None = None,
) -> AgentProfile:
    if clone_from is not None:
        source = await db.get(AgentProfile, clone_from)
        if source is None:
            raise CloneSourceNotFoundError(str(clone_from))
        if source.channel != "voice":
            raise CloneSourceNotFoundError(str(clone_from))
        draft = source.draft_config
    else:
        draft = DEFAULT_AGENT_CONFIG.model_dump()
    profile = AgentProfile(
        name=name,
        description=description,
        draft_config=draft,
        created_by=actor_email,
        updated_by=actor_email,
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return profile


async def get_profile(db: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    return await db.get(AgentProfile, profile_id)


async def list_profiles(
    db: AsyncSession, *, channel: Literal["voice", "chat"] | None = None
) -> list[AgentProfile]:
    stmt = select(AgentProfile).order_by(AgentProfile.name)
    if channel is not None:
        stmt = stmt.where(AgentProfile.channel == channel)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_draft(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    config: dict[str, Any],
    description: str | None,
    actor_email: str,
    expected_revision: int | None = None,
) -> AgentProfile | None:
    """Persist the draft config, bumping draft_revision.

    With ``expected_revision`` set, do a guarded conditional UPDATE
    (``WHERE id = :id AND draft_revision = :expected``). A 0-rowcount result is
    disambiguated by a re-SELECT: row absent -> return None (404); row present ->
    raise StaleDraftError (409). Omitting ``expected_revision`` keeps the old
    unconditional behavior (still bumps the revision) for backward compatibility.
    """
    if expected_revision is not None:
        values: dict[Any, Any] = {
            AgentProfile.draft_config: config,
            AgentProfile.updated_by: actor_email,
            AgentProfile.draft_revision: AgentProfile.draft_revision + 1,
            # Bulk update bypasses onupdate=func.now() — set it explicitly.
            AgentProfile.updated_at: func.now(),
        }
        if description is not None:
            values[AgentProfile.description] = description
        result = await db.execute(
            update(AgentProfile)
            .where(
                AgentProfile.id == profile_id,
                AgentProfile.draft_revision == expected_revision,
            )
            .values(values)
        )
        # execute() of a DML statement returns a CursorResult (has rowcount); the
        # statically-inferred Result type does not expose it.
        if result.rowcount == 0:  # type: ignore[attr-defined]
            # Re-SELECT to tell 404 (no such profile) from 409 (revision moved on).
            exists = await db.get(AgentProfile, profile_id)
            if exists is None:
                return None
            raise StaleDraftError(str(profile_id))
        await db.flush()
        # The bulk UPDATE bypasses the identity map; re-fetch the fresh row.
        db.expire_all()
        return await db.get(AgentProfile, profile_id)

    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    profile.draft_config = config
    if description is not None:
        profile.description = description
    profile.updated_by = actor_email
    profile.draft_revision = profile.draft_revision + 1
    await db.flush()
    await db.refresh(profile)
    return profile


async def _next_version(db: AsyncSession, profile_id: uuid.UUID) -> int:
    # NOTE: SELECT MAX + INSERT is not atomic. P1 is single-controller (admin UI
    # writes serialized through one process), so concurrent publishes do not occur.
    # If concurrent writers are ever added, take a row lock on the parent profile
    # (SELECT ... FOR UPDATE) or handle the unique-index IntegrityError with a retry.
    result = await db.execute(
        select(func.max(AgentProfileVersion.version)).where(
            AgentProfileVersion.profile_id == profile_id
        )
    )
    current = result.scalar_one_or_none()
    return (current or 0) + 1


async def publish(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    note: str | None,
    actor_email: str,
) -> AgentProfileVersion | None:
    # Concurrency note: version assignment (_next_version) is SELECT MAX + INSERT and
    # not atomic. P1 is single-controller so this cannot race; if a second writer is
    # added, the uq_(profile_id, version) index makes the loser raise IntegrityError
    # (currently surfacing as a 500) — handle it there with a row lock or 409 retry.
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    version_number = await _next_version(db, profile_id)
    version = AgentProfileVersion(
        profile_id=profile_id,
        version=version_number,
        config=profile.draft_config,
        note=note,
        published_by=actor_email,
    )
    db.add(version)
    profile.published_version = version_number
    profile.updated_by = actor_email
    # Bump the concurrency token so an editor holding a pre-publish revision is
    # told to reload (FR-032). rollback() funnels through here, so it bumps too.
    profile.draft_revision = profile.draft_revision + 1
    await db.flush()
    await db.refresh(version)
    return version


# Bound the version history read: publish/rollback append a row each time, so a
# long-lived profile's history grows monotonically. Default cap mirrors the audit
# repo (_MAX_LIST_LIMIT=500); newest-first so the cap keeps the most recent.
MAX_VERSIONS_LIMIT = 500


async def list_versions(
    db: AsyncSession, profile_id: uuid.UUID, *, limit: int = MAX_VERSIONS_LIMIT
) -> list[AgentProfileVersion]:
    result = await db.execute(
        select(AgentProfileVersion)
        .where(AgentProfileVersion.profile_id == profile_id)
        .order_by(AgentProfileVersion.version.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_version(
    db: AsyncSession, profile_id: uuid.UUID, version: int
) -> AgentProfileVersion | None:
    result = await db.execute(
        select(AgentProfileVersion).where(
            AgentProfileVersion.profile_id == profile_id,
            AgentProfileVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def delete_version(
    db: AsyncSession, profile_id: uuid.UUID, version: int
) -> AgentProfileVersion | None:
    """Hard-delete one historical AgentProfileVersion row for (profile_id, version).

    Returns the row if it existed, or None if no such row was found.
    AgentProfileVersion has no soft-delete/archived flag — hard delete matches
    the append-only history posture (published rows are permanent records; callers
    must guard against deleting the currently-published version).
    """
    row = await get_version(db, profile_id, version)
    if row is None:
        return None
    await db.delete(row)
    await db.flush()
    return row


async def rollback(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    target_version: int,
    actor_email: str,
) -> AgentProfileVersion | None:
    target = await get_version(db, profile_id, target_version)
    if target is None:
        return None
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    # Copy the target snapshot back into the draft, then publish it as a NEW
    # version so history stays append-only and linear.
    profile.draft_config = target.config
    profile.updated_by = actor_email
    await db.flush()
    return await publish(
        db, profile_id, note=f"rollback to v{target_version}", actor_email=actor_email
    )


async def set_default(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    direction: Literal["inbound", "outbound"],
) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    if profile.status == ProfileStatus.ARCHIVED:
        raise ProfileInUseError("cannot set an archived profile as default")
    if profile.channel != "voice":
        raise ProfileInUseError("cannot set a chat agent as a call default")
    column = (
        AgentProfile.is_default_inbound
        if direction == "inbound"
        else AgentProfile.is_default_outbound
    )
    # Clear the current holder first (the partial-unique index forbids two trues).
    # Set updated_at explicitly: onupdate=func.now() does NOT fire for bulk update().
    await db.execute(
        update(AgentProfile)
        .where(column.is_(True))
        .values({column: False, AgentProfile.updated_at: func.now()})
    )
    await db.flush()
    if direction == "inbound":
        profile.is_default_inbound = True
    else:
        profile.is_default_outbound = True
    await db.flush()
    await db.refresh(profile)
    return profile


async def count_assigned_contacts(db: AsyncSession, profile_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Contact).where(Contact.agent_profile_id == profile_id)
    )
    return int(result.scalar_one())


async def count_assigned_contacts_bulk(
    db: AsyncSession, profile_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Assigned-contact counts for many profiles in one grouped query.

    Replaces the per-profile :func:`count_assigned_contacts` fan-out in the list
    endpoint. Profiles with zero assignments are absent from the map (the GROUP BY
    emits no row); callers default missing ids to 0. Returns ``{}`` for empty input
    without touching the database.
    """
    if not profile_ids:
        return {}
    result = await db.execute(
        select(Contact.agent_profile_id, func.count())
        .where(Contact.agent_profile_id.in_(profile_ids))
        .group_by(Contact.agent_profile_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def unpublished_draft_flags_bulk(
    db: AsyncSession, profiles: Sequence[AgentProfile]
) -> dict[uuid.UUID, bool]:
    """has-unpublished-draft flag for many profiles, batching the version lookup.

    Mirrors :func:`has_unpublished_draft` exactly: a profile has an unpublished
    draft when it was never published, when its live published version row is
    missing, or when that row's config differs from the current draft. The single
    composite-key ``IN`` query fetches every live version at once instead of one
    SELECT per profile. Returns ``{}`` for empty input without touching the database.
    """
    if not profiles:
        return {}
    pairs = [(p.id, p.published_version) for p in profiles if p.published_version is not None]
    live_config_by_profile: dict[uuid.UUID, dict[str, Any]] = {}
    if pairs:
        result = await db.execute(
            select(AgentProfileVersion.profile_id, AgentProfileVersion.config).where(
                tuple_(AgentProfileVersion.profile_id, AgentProfileVersion.version).in_(pairs)
            )
        )
        live_config_by_profile = {row[0]: row[1] for row in result.all()}

    flags: dict[uuid.UUID, bool] = {}
    for p in profiles:
        if p.published_version is None:
            flags[p.id] = True
            continue
        live_config = live_config_by_profile.get(p.id)
        if live_config is None:
            flags[p.id] = True
            continue
        flags[p.id] = bool(live_config != p.draft_config)
    return flags


async def archive_profile(db: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    if profile.is_default_inbound or profile.is_default_outbound:
        raise ProfileInUseError("profile is a live default; clear the default first")
    if await count_assigned_contacts(db, profile_id) > 0:
        raise ProfileInUseError("profile is assigned to one or more contacts")
    profile.status = ProfileStatus.ARCHIVED
    await db.flush()
    await db.refresh(profile)
    return profile


async def has_unpublished_draft(db: AsyncSession, profile: AgentProfile) -> bool:
    if profile.published_version is None:
        return True
    live = await get_version(db, profile.id, profile.published_version)
    if live is None:
        return True
    return bool(live.config != profile.draft_config)


async def get_default_profile(
    db: AsyncSession, direction: Literal["inbound", "outbound"]
) -> AgentProfile | None:
    """The single ACTIVE profile marked default for this direction, or None.

    The partial-unique index guarantees at most one true per direction, so
    scalar_one_or_none() is safe.
    """
    column = (
        AgentProfile.is_default_inbound
        if direction == "inbound"
        else AgentProfile.is_default_outbound
    )
    result = await db.execute(
        select(AgentProfile).where(
            column.is_(True),
            AgentProfile.status == ProfileStatus.ACTIVE,
            AgentProfile.channel == "voice",
        )
    )
    return result.scalar_one_or_none()


async def get_default_holder(
    db: AsyncSession, direction: Literal["inbound", "outbound"]
) -> AgentProfile | None:
    """The profile flagged default for this direction, REGARDLESS of eligibility.

    Unlike :func:`get_default_profile` (which filters to ACTIVE), this returns the
    raw ``is_default_*`` holder even when it is archived or unpublished, so the
    Defaults area can surface an ineligible-default warning (FR-020). The
    partial-unique index guarantees at most one true per direction.
    """
    column = (
        AgentProfile.is_default_inbound
        if direction == "inbound"
        else AgentProfile.is_default_outbound
    )
    result = await db.execute(
        select(AgentProfile).where(column.is_(True), AgentProfile.channel == "voice")
    )
    return result.scalar_one_or_none()


async def get_published_config(
    db: AsyncSession, profile: AgentProfile
) -> AgentProfileVersion | None:
    """The live published version row for a profile, or None if never published."""
    if profile.published_version is None:
        return None
    return await get_version(db, profile.id, profile.published_version)


async def _resolved_from_profile(
    db: AsyncSession, profile: AgentProfile | None
) -> tuple[AgentProfile, AgentProfileVersion, AgentConfig] | None:
    """Resolve a single profile to a published, valid (profile, version, config) triple — or
    None to fall through to the next precedence tier.

    Returns None (so the caller tries the next precedence tier) when the profile is
    missing, archived, not a voice profile, unpublished, or its stored JSON fails
    validation. Never raises. Returning the raw ``version`` row (not just the parsed
    config) lets :func:`resolve_published_version` reuse this exact walk without a
    second profile/version load.
    """
    if profile is None or profile.status != ProfileStatus.ACTIVE or profile.channel != "voice":
        return None
    version = await get_published_config(db, profile)
    if version is None:
        return None
    try:
        config = AgentConfig.model_validate(version.config)
    except ValidationError:
        # A published, supposedly-live config no longer validates (e.g. a later schema
        # tightening invalidated an old snapshot). Falling through to the next tier can
        # silently degrade every call to defaults, so emit a STABLE event key (no PHI:
        # identity only) that a log-based alert can match — not just free-text.
        logger.bind(
            event="agent_config_validation_failed",
            profile_id=str(profile.id),
            version=version.version,
        ).warning("Published agent config failed validation; falling through to next tier")
        return None
    return profile, version, config


async def _resolve_winning_version(
    db: AsyncSession,
    *,
    profile_override: uuid.UUID | None,
    contact_profile_id: uuid.UUID | None,
    direction: Literal["inbound", "outbound"],
) -> tuple[AgentProfile, AgentProfileVersion, AgentConfig] | None:
    """The single precedence walk shared by :func:`resolve_agent_config` and
    :func:`resolve_published_version`: profile_override -> contact_profile_id -> direction
    default (:func:`get_default_profile`). Each candidate is resolved via
    :func:`_resolved_from_profile` (ACTIVE, voice, published, valid config); a candidate that
    fails any of those checks falls through to the next tier. Returns None when nothing
    resolves. Factoring the walk here (instead of in each caller) means the "pick a profile"
    logic exists in exactly one place.
    """
    for candidate_id in (profile_override, contact_profile_id):
        if candidate_id is None:
            continue
        profile = await get_profile(db, candidate_id)
        resolved = await _resolved_from_profile(db, profile)
        if resolved is not None:
            return resolved
    default_profile = await get_default_profile(db, direction)
    return await _resolved_from_profile(db, default_profile)


async def resolve_agent_config(
    db: AsyncSession,
    *,
    profile_override: uuid.UUID | None,
    contact_profile_id: uuid.UUID | None,
    direction: Literal["inbound", "outbound"],
) -> ResolvedAgentConfig | None:
    """Resolve the published config by precedence: override -> contact -> direction default.

    Each candidate must be ACTIVE and have a published, valid version; otherwise the
    walk falls through. Returns None when nothing resolves (the router then returns
    DEFAULT_AGENT_CONFIG).
    """
    winner = await _resolve_winning_version(
        db,
        profile_override=profile_override,
        contact_profile_id=contact_profile_id,
        direction=direction,
    )
    if winner is None:
        return None
    profile, version, config = winner
    return ResolvedAgentConfig(
        source="resolved", profile_id=profile.id, version=version.version, config=config
    )


async def resolve_published_version(
    db: AsyncSession,
    *,
    profile_override: uuid.UUID | None,
    contact_profile_id: uuid.UUID | None,
    direction: Literal["inbound", "outbound"],
) -> AgentProfileVersion | None:
    """The published AgentProfileVersion the SAME precedence walk as :func:`resolve_agent_config`
    selects (profile_override -> contact_profile_id -> direction default), WITHOUT parsing to
    AgentConfig — so callers that need the RAW ``version.config`` (e.g. flow_runtime_voice's
    compat_response_engine binding, which AgentConfig(extra="ignore") would strip) don't have to
    re-fetch profile + published version a second time. Delegates to the same private
    :func:`_resolve_winning_version` walk resolve_agent_config uses, so the two can never select
    different winners.
    """
    winner = await _resolve_winning_version(
        db,
        profile_override=profile_override,
        contact_profile_id=contact_profile_id,
        direction=direction,
    )
    return winner[1] if winner is not None else None


@dataclass(frozen=True)
class ResolvedPolicy:
    """The effective dialing policy for one call, with defaults filled in.

    ``start_local``/``end_local`` are parsed ``datetime.time`` quiet-hours
    bounds (statutory [09:00, 21:00) unless a profile narrows them);
    ``delay_multiplier`` scales every retry-ladder rung. Per-status retry caps
    live behind ``max_attempts_for`` (chain-global semantics, spec §3.3.1).
    """

    start_local: time
    end_local: time
    delay_multiplier: float
    _max_attempts: Mapping[CallStatus, int | None] = field(default_factory=dict)

    def max_attempts_for(self, status: CallStatus) -> int | None:
        """Chain-global retry cap for ``status``; None keeps the builtin ladder."""
        return self._max_attempts.get(status)


# Statutory TCPA defaults — what resolves when no profile carries a policy.
# Bounds derive from quiet_hours' exported constants so the two stay in sync.
STATUTORY_POLICY = ResolvedPolicy(
    start_local=time(QUIET_START_HOUR, 0),
    end_local=time(QUIET_END_HOUR, 0),
    delay_multiplier=1.0,
)


async def resolve_call_policy(
    db: AsyncSession,
    *,
    profile_override: uuid.UUID | None,
    contact_profile_id: uuid.UUID | None,
    direction: Literal["inbound", "outbound"],
) -> ResolvedPolicy:
    """Resolve the effective policy by precedence: override -> contact -> direction default.

    Thin wrapper over resolve_agent_config: the policy comes from the SAME
    profile that walk picks — whole-profile precedence, never per-field merge
    (spec §3.3.2). If the winning profile resolves but carries ``policy=None``,
    the result is STATUTORY_POLICY even when a lower-precedence profile
    narrows; attaching a policy-less ``profile_override`` therefore loosens
    quiet hours back to statutory relative to the contact's profile — still
    within the TCPA bound by construction (PolicyConfig validates
    narrowing-only).

    Re-resolved at EVERY consumption site (retry scheduling, the dial-moment
    quiet-hours re-check, both materialization clamps) and never snapshotted
    onto the Call: quiet hours are a TCPA compliance control, so a tightening
    publish must bind calls already queued (dial-time truth, spec §3.3.2).
    Caching is Open Q8 — not needed at eldercare volumes.
    """
    resolved = await resolve_agent_config(
        db,
        profile_override=profile_override,
        contact_profile_id=contact_profile_id,
        direction=direction,
    )
    if resolved is None or resolved.config.policy is None:
        return STATUTORY_POLICY
    policy = resolved.config.policy
    rma = policy.retry_max_attempts
    max_attempts: dict[CallStatus, int | None] = (
        {}
        if rma is None
        else {
            CallStatus.NO_ANSWER: rma.no_answer,
            CallStatus.VOICEMAIL_LEFT: rma.voicemail_left,
            CallStatus.BUSY: rma.busy,
            CallStatus.FAILED: rma.failed,
        }
    )
    # "HH:MM" strings are already format+narrowing validated by PolicyConfig;
    # unset sides stay statutory (each may be narrowed independently).
    return ResolvedPolicy(
        start_local=(
            time.fromisoformat(policy.quiet_hours_start_local)
            if policy.quiet_hours_start_local is not None
            else STATUTORY_POLICY.start_local
        ),
        end_local=(
            time.fromisoformat(policy.quiet_hours_end_local)
            if policy.quiet_hours_end_local is not None
            else STATUTORY_POLICY.end_local
        ),
        delay_multiplier=(
            policy.retry_delay_multiplier if policy.retry_delay_multiplier is not None else 1.0
        ),
        _max_attempts=max_attempts,
    )


async def is_live_profile(
    db: AsyncSession, profile_id: uuid.UUID, *, channel: Literal["voice", "chat"] | None = None
) -> bool:
    """True iff the profile exists, is ACTIVE, has a published version (the precondition for
    profile_override to take effect, spec §4) and — when ``channel`` is given — matches it, so a
    chat agent passed as a voice override is rejected."""
    profile = await get_profile(db, profile_id)
    return (
        profile is not None
        and profile.status is ProfileStatus.ACTIVE
        and profile.published_version is not None
        and (channel is None or profile.channel == channel)
    )


async def list_profiles_keyset(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[AgentProfile]:
    """Keyset-paginate non-archived profiles over (created_at, id) — v2 list-retell-llms.

    Channel-agnostic on purpose (a Retell-LLM is channel-agnostic infra, 4c-1). RLS scopes
    to the caller's org. Fetches limit+1 so the caller computes has_more without a COUNT.
    """
    stmt = select(AgentProfile).where(AgentProfile.status != ProfileStatus.ARCHIVED)
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    AgentProfile.created_at < after_created_at,
                    and_(
                        AgentProfile.created_at == after_created_at,
                        AgentProfile.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    AgentProfile.created_at > after_created_at,
                    and_(
                        AgentProfile.created_at == after_created_at,
                        AgentProfile.id > after_id,
                    ),
                )
            )
    order = (
        (AgentProfile.created_at.desc(), AgentProfile.id.desc())
        if descending
        else (AgentProfile.created_at.asc(), AgentProfile.id.asc())
    )
    stmt = stmt.order_by(*order).limit(limit + 1)
    result = await db.execute(stmt)
    return list(result.scalars().all())
