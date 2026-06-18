from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import get_tenant_db, require_super_admin
from usan_api.repositories import agent_profiles as repo
from usan_api.schemas.admin_defaults import DefaultsResponse, DirectionDefault

_DIRECTIONS: tuple[Literal["inbound", "outbound"], ...] = ("inbound", "outbound")

router = APIRouter(
    prefix="/v1/admin/defaults",
    tags=["admin-defaults"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("", response_model=DefaultsResponse)
async def get_defaults(db: AsyncSession = Depends(get_tenant_db)) -> DefaultsResponse:
    """Per-direction default state + the read-only built-in fallback (US3, FR-016..020).

    Read-only. For each direction returns the flagged default profile (name/id only)
    and whether it still resolves for a call; an ineligible (archived/unpublished)
    default is surfaced so the Defaults area can prompt for a replacement (FR-020).
    The resolution-order descriptor and the built-in DEFAULT_AGENT_CONFIG come from
    the response schema constants — names/config only, never per-call PHI (spec §7).
    ``get_default_holder`` (not ``get_default_profile``) is used so an ineligible
    holder is still visible.
    """
    directions = [
        DirectionDefault.from_holder(direction, await repo.get_default_holder(db, direction))
        for direction in _DIRECTIONS
    ]
    return DefaultsResponse(directions=directions)
