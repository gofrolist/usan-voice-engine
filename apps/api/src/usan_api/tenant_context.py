"""Per-transaction tenant context for RLS.

set_tenant_context issues set_config('app.current_org', <uuid>, is_local=true) — the
parameterizable equivalent of SET LOCAL, transaction-scoped, so the value never leaks
across pooled connections. RLS policies read it via current_setting('app.current_org',
true); unset => NULL => zero rows (fail-closed).

In P1 the resolver always returns the single seeded default org, so every existing
caller is transparently scoped to one org and behavior is unchanged. P2 replaces the
resolver with "the authenticated user's org / act-as target".
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.repositories.organizations import get_org_by_slug
from usan_api.settings import get_settings

# Process-lifetime cache: the default org is seeded once (migration 0030) and its id is
# immutable in P1, so caching is safe. A slug/id change requires a process restart to take
# effect; _clear_default_org_cache() is test-only (per-test isolation teardown).
_default_org_id: uuid.UUID | None = None


async def resolve_default_org_id(db: AsyncSession) -> uuid.UUID:
    """The seeded default org's id, cached after first lookup."""
    global _default_org_id
    if _default_org_id is None:
        org = await get_org_by_slug(db, get_settings().default_org_slug)
        if org is None:
            raise RuntimeError("default organization is not seeded")
        _default_org_id = org.id
    return _default_org_id


def _clear_default_org_cache() -> None:
    """Reset the cached default-org id (TEST-ONLY: per-test isolation teardown)."""
    global _default_org_id
    _default_org_id = None


async def set_tenant_context(db: AsyncSession, org_id: uuid.UUID) -> None:
    # is_local=true => transaction-scoped, cleared at COMMIT/ROLLBACK; no cross-request leak.
    await db.execute(
        text("SELECT set_config('app.current_org', :org, true)"),
        {"org": str(org_id)},
    )
