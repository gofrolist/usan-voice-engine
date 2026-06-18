"""Admin CRUD for custom variable definitions (spec §5).

Copies the ``admin_users.py`` precedent: router-level session gate, ADMIN-role
write gating, ``admin_audit.record`` before the single commit. Definitions are
documentation/UX only — values arrive per call via ``Call.dynamic_vars``, so
audit detail carries names/flags only, never values (spec §7). ``name`` is
immutable after create (``CustomVariableUpdate`` forbids it → 422); DELETE is a
hard delete with no referential scan — tokens referencing the name revert to
unknown-token warnings (spec §4). The existing ``/v1/admin`` rate limiting
covers this prefix (``ratelimit.py``).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_super_admin
from usan_api.db.base import AdminRole
from usan_api.db.models import CustomVariable
from usan_api.repositories import admin_audit
from usan_api.repositories import custom_variables as repo
from usan_api.schemas.custom_variables import (
    CustomVariableCreate,
    CustomVariableOut,
    CustomVariableReferences,
    CustomVariableUpdate,
    VariableReference,
)

router = APIRouter(
    prefix="/v1/admin/custom-variables",
    tags=["custom-variables"],
    dependencies=[Depends(require_super_admin)],
)

_NOT_FOUND = "custom variable not found"


def _to_out(row: CustomVariable) -> CustomVariableOut:
    return CustomVariableOut.from_model(row)


@router.get("", response_model=list[CustomVariableOut])
async def list_custom_variables(
    db: AsyncSession = Depends(get_tenant_db),
) -> list[CustomVariableOut]:
    """All definitions, alphabetical — readable by every session role."""
    return [_to_out(v) for v in await repo.list_custom_variables(db)]


@router.get("/{variable_id}/references", response_model=CustomVariableReferences)
async def custom_variable_references(
    variable_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> CustomVariableReferences:
    """Delete-guard (FR-007): profiles referencing this variable's {{name}} token.

    Scans the live draft AND every published version across the prompt fields +
    SMS bodies. Readable by every session role (a read). Names/locations only —
    never prompt text or per-call values (spec §7).
    """
    row = await repo.get_custom_variable(db, variable_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    refs = await repo.references_to(db, row.name)
    return CustomVariableReferences(profiles=[VariableReference(**r) for r in refs])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CustomVariableOut)
async def create_custom_variable(
    body: CustomVariableCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> CustomVariableOut:
    try:
        row = await repo.create_custom_variable(
            db, name=body.name, description=body.description, example=body.example, phi=body.phi
        )
    except repo.DuplicateCustomVariableError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await admin_audit.record(
        db,
        actor_email=actor,
        action="custom_variable.create",
        entity_type="custom_variable",
        entity_id=str(row.id),
        detail={"name": row.name, "phi": row.phi},
    )
    await db.commit()
    return _to_out(row)


@router.patch("/{variable_id}", response_model=CustomVariableOut)
async def update_custom_variable(
    variable_id: uuid.UUID,
    body: CustomVariableUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> CustomVariableOut:
    row = await repo.get_custom_variable(db, variable_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    old_phi = row.phi
    # exclude_unset + drop-None: an explicit null means "leave as is", like absent.
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    row = await repo.update_custom_variable(db, row, **fields)
    # Changed-field names + old/new on phi transitions (flips are allowed in both
    # directions, silently but audited — spec §5). Names/flags only, never values.
    detail: dict[str, Any] = {"name": row.name, "changed": sorted(fields)}
    if "phi" in fields and row.phi != old_phi:
        detail["phi"] = {"old": old_phi, "new": row.phi}
    await admin_audit.record(
        db,
        actor_email=actor,
        action="custom_variable.update",
        entity_type="custom_variable",
        entity_id=str(variable_id),
        detail=detail,
    )
    await db.commit()
    return _to_out(row)


@router.delete("/{variable_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_variable(
    variable_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    row = await repo.get_custom_variable(db, variable_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    name = row.name
    await repo.delete_custom_variable(db, row)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="custom_variable.delete",
        entity_type="custom_variable",
        entity_id=str(variable_id),
        detail={"name": name},
    )
    await db.commit()
