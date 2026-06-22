"""Model catalog endpoint (US2 / FR-011, FR-012, research R2).

``GET /v1/admin/model-catalog`` returns the curated LLM + STT model allow-list
(mirrors ``admin_tool_catalog`` / ``admin_voice_catalog``). Read-only; backs the
curated ``LLMSection``/``STTSection`` selects in the editor.
"""

from fastapi import APIRouter, Depends

from usan_api.auth import require_admin_role
from usan_api.db.base import AdminRole
from usan_api.schemas.model_catalog import MODEL_CATALOG, ModelCatalogResponse

router = APIRouter(
    prefix="/v1/admin/model-catalog",
    tags=["admin-model-catalog"],
    dependencies=[Depends(require_admin_role(AdminRole.VIEWER))],
)


@router.get("", response_model=ModelCatalogResponse)
async def get_model_catalog() -> ModelCatalogResponse:
    """Return the curated LLM + STT model catalog (FR-011, FR-012).

    Readable by any authenticated org member. The catalog is a global constant (a
    platform-curated allow-list), not per-version snapshot data.
    """
    return ModelCatalogResponse(models=list(MODEL_CATALOG))
