from fastapi import APIRouter, Depends

from usan_api.auth import require_super_admin
from usan_api.schemas.tool_catalog import TOOL_CATALOG, ToolCatalogResponse

router = APIRouter(
    prefix="/v1/admin/tool-catalog",
    tags=["admin-tool-catalog"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("", response_model=ToolCatalogResponse)
async def get_tool_catalog() -> ToolCatalogResponse:
    """Return the global tool catalog for the agent-config editor (design §4.1).

    Operator-only (super-admin) scope, mirroring admin_variable_catalog. The catalog is
    a global constant (a closed, code-backed inventory), not per-version snapshot data; it is
    the single source of truth the frontend uses to render the tool toggles and the
    editor enforces as a hard set (unknown tool names are rejected, not warned).
    """
    return ToolCatalogResponse(tools=list(TOOL_CATALOG))
