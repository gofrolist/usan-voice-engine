"""Task C2: the super-admin org console (/v1/admin/organizations).

Platform-level (global, non-RLS) control plane: a USAN staffer lists every org and
provisions a new one (optionally seeding its first ADMIN). These endpoints touch the
GLOBAL organizations + memberships tables, so the router depends on
``require_super_admin`` + ``get_db`` (the default-org connect baseline), NOT
``get_tenant_db`` — there is no single active org for a cross-org console.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import AdminPrincipal, require_super_admin
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import memberships as members_repo
from usan_api.repositories import organizations as orgs_repo
from usan_api.schemas.organizations import OrgCreate, OrgOut

router = APIRouter(
    prefix="/v1/admin/organizations",
    tags=["organizations"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("", response_model=list[OrgOut])
async def list_orgs(db: AsyncSession = Depends(get_db)) -> list[OrgOut]:
    return [
        OrgOut(id=o.id, name=o.name, slug=o.slug, status=o.status)
        for o in await orgs_repo.list_orgs(db)
    ]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrgOut)
async def create_org(
    body: OrgCreate,
    principal: AdminPrincipal = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    try:
        org = await orgs_repo.create_org(db, name=body.name, slug=body.slug)
        await db.flush()
        if body.first_admin_email:
            await members_repo.add_member(
                db,
                email=body.first_admin_email,
                org_id=org.id,
                role=AdminRole.ADMIN,
                added_by=principal.email,
            )
        await admin_audit.record(
            db,
            actor_email=principal.email,
            action="org.create",
            entity_type="organization",
            entity_id=str(org.id),
            detail={"slug": body.slug},
        )
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already exists") from e
    return OrgOut(id=org.id, name=org.name, slug=org.slug, status=org.status)
