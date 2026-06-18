from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import get_tenant_db, require_super_admin
from usan_api.repositories import custom_variables as custom_variables_repo
from usan_api.schemas.variable_catalog import BUILTIN_NAMES, BUILTIN_VARIABLES, VariableSpec

router = APIRouter(
    prefix="/v1/admin/variable-catalog",
    tags=["admin-variable-catalog"],
    dependencies=[Depends(require_super_admin)],
)


class VariableCatalogResponse(BaseModel):
    variables: list[VariableSpec]


@router.get("", response_model=VariableCatalogResponse)
async def get_variable_catalog(
    db: AsyncSession = Depends(get_tenant_db),
) -> VariableCatalogResponse:
    """Return the variable catalog for the prompt-editor palette (design §4.6).

    Operator-only (super-admin) scope, mirroring the other operator routers. DB-backed since
    Phase A4 (spec §3.2): the 10 builtins in canonical order, then declared
    customs alphabetical, mapped to ``tier="custom"`` with ``default=""`` —
    **definitions carry no values**; values arrive per call via
    ``Call.dynamic_vars``. The catalog stays global (not per-contact PHI, not a
    per-version snapshot); it is the single source of truth the frontend uses
    to render the insert-variable chips and flag unknown tokens.
    """
    customs: list[VariableSpec] = []
    for row in await custom_variables_repo.list_custom_variables(db):
        if row.name in BUILTIN_NAMES:
            # Future-builtin collision: create-time validation only knows the
            # builtins of its day, so a custom row can be shadowed by a builtin
            # added later. Drop it so the merged catalog never carries duplicate
            # names (spec §3.2). Name only in the log — never values (spec §7).
            logger.bind(name=row.name).warning(
                "custom variable {name} shadowed by builtin; ignored", name=row.name
            )
            continue
        customs.append(
            VariableSpec(
                name=row.name,
                tier="custom",
                description=row.description,
                default="",
                example=row.example,
                phi=row.phi,
            )
        )
    return VariableCatalogResponse(variables=list(BUILTIN_VARIABLES) + customs)
