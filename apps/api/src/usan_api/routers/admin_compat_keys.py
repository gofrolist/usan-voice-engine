"""Native /v1/admin endpoints to issue / list / revoke compat API keys (feature 003).

Part of the NATIVE control plane (not the RetellAI surface): super-admin guarded, uses the
admin session + the org-scoped tenant DB. The plaintext token is returned ONCE at create
(never stored, never re-readable) — mirroring the per-endpoint webhook signing secret.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import AdminPrincipal, get_tenant_db, require_super_admin
from usan_api.repositories import compat_api_keys as keys_repo
from usan_api.schemas.compat_api_keys import (
    CompatKeyCreatedResponse,
    CompatKeyCreateRequest,
    CompatKeyResponse,
)

router = APIRouter(
    prefix="/v1/admin/compat-keys",
    tags=["admin-compat-keys"],
    dependencies=[Depends(require_super_admin)],
)


@router.post("", response_model=CompatKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_compat_key(
    body: CompatKeyCreateRequest,
    principal: AdminPrincipal = Depends(require_super_admin),
    db: AsyncSession = Depends(get_tenant_db),
) -> CompatKeyCreatedResponse:
    # get_tenant_db 409s when no org is active, so active_org_id is set here.
    assert principal.active_org_id is not None
    issued = await keys_repo.create(db, organization_id=principal.active_org_id, label=body.label)
    await db.commit()
    return CompatKeyCreatedResponse.from_issued(issued)


@router.get("", response_model=list[CompatKeyResponse])
async def list_compat_keys(
    principal: AdminPrincipal = Depends(require_super_admin),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[CompatKeyResponse]:
    assert principal.active_org_id is not None
    rows = await keys_repo.list_for_org(db, principal.active_org_id)
    return [CompatKeyResponse.from_row(r) for r in rows]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_compat_key(
    key_id: uuid.UUID,
    principal: AdminPrincipal = Depends(require_super_admin),
    db: AsyncSession = Depends(get_tenant_db),
) -> None:
    assert principal.active_org_id is not None
    revoked = await keys_repo.revoke(db, key_id, principal.active_org_id)
    if revoked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="compat key not found")
    await db.commit()
