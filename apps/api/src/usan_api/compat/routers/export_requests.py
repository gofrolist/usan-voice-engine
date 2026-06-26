"""RetellAI-compat analytics/export surface (Phase 2): list-export-requests only.

The oracle exposes NO create/get-by-id export op — a parity client cannot enqueue an export
through the API, so a fresh org genuinely has none. This is a shape-conformant empty-list stub
(no table/poller/GCS). A real async export job is a documented follow-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from loguru import logger

router = APIRouter(tags=["compat-export-requests"])


def _audit(request: Request, op: str) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat export op={op}")


@router.get("/v2/list-export-requests")
async def list_export_requests(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
) -> dict[str, Any]:
    # Params accepted + validated, then ignored: there are no export rows to page.
    _audit(request, "list-export-requests")
    return {"items": [], "has_more": False}
