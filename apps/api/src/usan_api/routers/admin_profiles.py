import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import CloneSourceNotFoundError, ProfileInUseError
from usan_api.schemas.agent_config import unknown_tokens
from usan_api.schemas.agent_profile import (
    DraftUpdate,
    ProfileCreate,
    ProfileDetail,
    ProfileSummary,
    PublishRequest,
    SetDefaultRequest,
    VersionDetail,
    VersionSummary,
)

router = APIRouter(
    prefix="/v1/admin/profiles",
    tags=["admin-profiles"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=list[ProfileSummary])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[ProfileSummary]:
    profiles = await repo.list_profiles(db)
    summaries: list[ProfileSummary] = []
    # TODO(perf): N+1 — has_unpublished_draft + count_assigned_elders run per profile.
    # Batch into a single join/group-by query when the profile list grows.
    for p in profiles:
        summaries.append(
            ProfileSummary.from_model(
                p,
                has_unpublished_draft=await repo.has_unpublished_draft(db, p),
                assigned_elder_count=await repo.count_assigned_elders(db, p.id),
            )
        )
    return summaries


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileSummary)
async def create_profile(
    body: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ProfileSummary:
    try:
        profile = await repo.create_profile(
            db,
            name=body.name,
            description=body.description,
            actor_email=actor,
            clone_from=body.clone_from,
        )
        await admin_audit.record(
            db,
            actor_email=actor,
            action="profile.create",
            entity_type="agent_profile",
            entity_id=str(profile.id),
            detail={"name": body.name},
        )
        await db.commit()
    except CloneSourceNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="clone_from profile not found") from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="profile name already exists") from exc
    await db.refresh(profile)
    return ProfileSummary.from_model(profile, has_unpublished_draft=True, assigned_elder_count=0)


@router.get("/{profile_id}", response_model=ProfileDetail)
async def get_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ProfileDetail:
    profile = await repo.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return ProfileDetail.from_model(profile)


@router.put("/{profile_id}/draft", response_model=ProfileDetail)
async def update_draft(
    profile_id: uuid.UUID,
    body: DraftUpdate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ProfileDetail:
    profile = await repo.update_draft(
        db,
        profile_id,
        config=body.config.model_dump(),
        description=body.description,
        actor_email=actor,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    # Compute non-fatal unknown-{{var}} warnings across every prompt field so the
    # editor can flag them (warn-don't-block, design §5.1). The save itself already
    # succeeded — unknown tokens never fail validation.
    prompts = body.config.prompts
    seen: list[str] = []
    for text in (
        prompts.system_prompt,
        prompts.greeting,
        prompts.recording_disclosure,
        prompts.voicemail_message,
        prompts.checkin_flow_instructions,
        prompts.goodbye_message,
        prompts.inbound_opening,
        prompts.inbound_personalization_template,
    ):
        for name in unknown_tokens(text):
            if name not in seen:
                seen.append(name)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.draft_update",
        entity_type="agent_profile",
        entity_id=str(profile_id),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile, warnings=seen)


@router.post(
    "/{profile_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionSummary,
)
async def publish(
    profile_id: uuid.UUID,
    body: PublishRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> VersionSummary:
    version = await repo.publish(db, profile_id, note=body.note, actor_email=actor)
    if version is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.publish",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"version": version.version},
    )
    await db.commit()
    await db.refresh(version)
    return VersionSummary.from_model(version)


@router.get("/{profile_id}/versions", response_model=list[VersionSummary])
async def list_versions(
    profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[VersionSummary]:
    if await repo.get_profile(db, profile_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    versions = await repo.list_versions(db, profile_id)
    return [VersionSummary.from_model(v) for v in versions]


@router.get("/{profile_id}/versions/{version}", response_model=VersionDetail)
async def get_version(
    profile_id: uuid.UUID, version: int, db: AsyncSession = Depends(get_db)
) -> VersionDetail:
    row = await repo.get_version(db, profile_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="version not found")
    return VersionDetail.from_model(row)


@router.post(
    "/{profile_id}/rollback/{version}",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionSummary,
)
async def rollback(
    profile_id: uuid.UUID,
    version: int,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> VersionSummary:
    new_version = await repo.rollback(db, profile_id, target_version=version, actor_email=actor)
    if new_version is None:
        raise HTTPException(status_code=404, detail="profile or version not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.rollback",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"from_version": version, "new_version": new_version.version},
    )
    await db.commit()
    await db.refresh(new_version)
    return VersionSummary.from_model(new_version)


@router.post("/{profile_id}/set-default", response_model=ProfileDetail)
async def set_default(
    profile_id: uuid.UUID,
    body: SetDefaultRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ProfileDetail:
    try:
        profile = await repo.set_default(db, profile_id, direction=body.direction)
    except ProfileInUseError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.set_default",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"direction": body.direction},
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile)


@router.post("/{profile_id}/archive", response_model=ProfileDetail)
async def archive(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ProfileDetail:
    try:
        profile = await repo.archive_profile(db, profile_id)
    except ProfileInUseError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.archive",
        entity_type="agent_profile",
        entity_id=str(profile_id),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile)
