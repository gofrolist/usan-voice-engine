import uuid
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion, Elder
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG


class ProfileInUseError(Exception):
    """Raised when archiving a profile that is still a default or assigned to elders."""


class CloneSourceNotFoundError(Exception):
    """Raised when create_profile(clone_from=...) references a profile that doesn't exist."""


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


async def list_profiles(db: AsyncSession) -> list[AgentProfile]:
    result = await db.execute(select(AgentProfile).order_by(AgentProfile.name))
    return list(result.scalars().all())


async def update_draft(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    config: dict[str, Any],
    description: str | None,
    actor_email: str,
) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    profile.draft_config = config
    if description is not None:
        profile.description = description
    profile.updated_by = actor_email
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
    await db.flush()
    await db.refresh(version)
    return version


async def list_versions(db: AsyncSession, profile_id: uuid.UUID) -> list[AgentProfileVersion]:
    result = await db.execute(
        select(AgentProfileVersion)
        .where(AgentProfileVersion.profile_id == profile_id)
        .order_by(AgentProfileVersion.version.desc())
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


async def count_assigned_elders(db: AsyncSession, profile_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Elder).where(Elder.agent_profile_id == profile_id)
    )
    return int(result.scalar_one())


async def archive_profile(db: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    if profile.is_default_inbound or profile.is_default_outbound:
        raise ProfileInUseError("profile is a live default; clear the default first")
    if await count_assigned_elders(db, profile_id) > 0:
        raise ProfileInUseError("profile is assigned to one or more elders")
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
