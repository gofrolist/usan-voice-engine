from fastapi import APIRouter, Depends
from pydantic import BaseModel

from usan_api.auth import require_admin_session
from usan_api.schemas.variable_catalog import BUILTIN_VARIABLES, VariableSpec

router = APIRouter(
    prefix="/v1/admin/variable-catalog",
    tags=["admin-variable-catalog"],
    dependencies=[Depends(require_admin_session)],
)


class VariableCatalogResponse(BaseModel):
    variables: list[VariableSpec]


@router.get("", response_model=VariableCatalogResponse)
async def get_variable_catalog() -> VariableCatalogResponse:
    """Return the global variable catalog for the prompt-editor palette (design §4.6).

    Admin-session scope, mirroring the other /v1/admin routers. The catalog is a
    global constant, not per-elder PHI; it is the single source of truth the
    frontend uses to render the insert-variable chips and flag unknown tokens.
    """
    return VariableCatalogResponse(variables=list(BUILTIN_VARIABLES))
