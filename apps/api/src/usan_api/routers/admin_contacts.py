import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import Contact
from usan_api.masking import mask_phone
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.schemas.admin import (
    AssignProfileRequest,
    ContactCreate,
    ContactDetail,
    ContactSummary,
    ContactUpdate,
    SetTimezoneRequest,
)

router = APIRouter(
    prefix="/v1/admin/contacts",
    tags=["admin-contacts"],
    dependencies=[Depends(require_admin_session)],
)


def _summary(contact: Contact, profile_name: str | None) -> ContactSummary:
    return ContactSummary(
        id=contact.id,
        name=contact.name,
        masked_phone=mask_phone(contact.phone_e164),
        timezone=contact.timezone,
        agent_profile_id=contact.agent_profile_id,
        agent_profile_name=profile_name,
    )


@router.get("", response_model=list[ContactSummary])
async def list_contacts(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[ContactSummary]:
    # Paged: the roster can be large, so never select the whole table at once.
    rows = await contacts_repo.list_with_profile(db, limit=limit, offset=offset)
    return [_summary(e, name) for e, name in rows]


@router.put("/{contact_id}/profile", response_model=ContactSummary)
async def assign_profile(
    contact_id: uuid.UUID,
    body: AssignProfileRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactSummary:
    try:
        contact = await contacts_repo.assign_profile(db, contact_id, body.agent_profile_id)
        if contact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
        await admin_audit.record(
            db,
            actor_email=actor,
            action="contact.assign_profile",
            entity_type="contact",
            entity_id=str(contact_id),
            detail={
                "agent_profile_id": str(body.agent_profile_id) if body.agent_profile_id else None
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="unknown agent_profile_id") from exc
    profile_name = None
    if contact.agent_profile_id is not None:
        prof = await profiles_repo.get_profile(db, contact.agent_profile_id)
        profile_name = prof.name if prof else None
    return _summary(contact, profile_name)


def _detail(contact: Contact, profile_name: str | None) -> ContactDetail:
    return ContactDetail(
        id=contact.id,
        name=contact.name,
        masked_phone=mask_phone(contact.phone_e164),
        timezone=contact.timezone,
        agent_profile_id=contact.agent_profile_id,
        agent_profile_name=profile_name,
        external_id=contact.external_id,
        preferred_voice=contact.preferred_voice,
        metadata=contact.meta,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )


async def _profile_name(db: AsyncSession, contact: Contact) -> str | None:
    if contact.agent_profile_id is None:
        return None
    prof = await profiles_repo.get_profile(db, contact.agent_profile_id)
    return prof.name if prof else None


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ContactDetail)
async def create_contact(
    body: ContactCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactDetail:
    try:
        contact = await contacts_repo.create_contact(
            db,
            name=body.name,
            phone_e164=body.phone_e164,
            timezone=body.timezone,
            external_id=body.external_id,
            preferred_voice=body.preferred_voice,
            metadata=body.metadata,
        )
        await admin_audit.record(
            db,
            actor_email=actor,
            action="contact.create",
            entity_type="contact",
            entity_id=str(contact.id),
            detail={"has_external_id": body.external_id is not None},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="phone_e164 or external_id already in use"
        ) from exc
    await db.refresh(contact)
    return _detail(contact, await _profile_name(db, contact))


@router.get("/{contact_id}", response_model=ContactDetail)
async def get_contact_detail(
    contact_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> ContactDetail:
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return _detail(contact, await _profile_name(db, contact))


@router.patch("/{contact_id}", response_model=ContactDetail)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactDetail:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    try:
        contact = await contacts_repo.update_contact(db, contact_id, fields)
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        await admin_audit.record(
            db,
            actor_email=actor,
            action="contact.update",
            entity_type="contact",
            entity_id=str(contact_id),
            detail={"fields": sorted(fields.keys())},  # field NAMES only, never values
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="phone_e164 or external_id already in use"
        ) from exc
    await db.refresh(contact)
    return _detail(contact, await _profile_name(db, contact))


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    deleted = await contacts_repo.delete_contact(db, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="contact not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="contact.delete",
        entity_type="contact",
        entity_id=str(contact_id),
    )
    await db.commit()


@router.put("/{contact_id}/timezone", response_model=ContactSummary)
async def set_timezone(
    contact_id: uuid.UUID,
    body: SetTimezoneRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactSummary:
    # No IntegrityError guard (unlike assign_profile, which can hit an FK violation
    # on agent_profile_id): timezone is a plain Text column with no FK/unique
    # constraint, and SetTimezoneRequest has already IANA-validated the value.
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    old = contact.timezone
    # set_timezone mutates this same session-cached instance; ignore its return value.
    await contacts_repo.set_timezone(db, contact_id, body.timezone)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="contact.set_timezone",
        entity_type="contact",
        entity_id=str(contact_id),
        detail={"old": old, "new": body.timezone},
    )
    await db.commit()
    profile_name = None
    if contact.agent_profile_id is not None:
        prof = await profiles_repo.get_profile(db, contact.agent_profile_id)
        profile_name = prof.name if prof else None
    return _summary(contact, profile_name)
