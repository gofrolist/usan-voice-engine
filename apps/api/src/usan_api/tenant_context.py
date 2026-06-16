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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.repositories.organizations import get_org_by_slug
from usan_api.settings import get_settings

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


async def set_tenant_context(db: AsyncSession, org_id: uuid.UUID) -> None:
    # is_local=true => transaction-scoped, cleared at COMMIT/ROLLBACK; no cross-request leak.
    await db.execute(
        text("SELECT set_config('app.current_org', :org, true)"),
        {"org": str(org_id)},
    )


@asynccontextmanager
async def session_in_default_org() -> AsyncIterator[AsyncSession]:
    """A session with tenant context pre-set to the default org, for background workers.

    P1 single-org behavior. P2 will replace this with per-org iteration (Open Q1).
    """
    from usan_api.db.session import get_session_factory

    async with get_session_factory()() as session:
        org_id = await resolve_default_org_id(session)
        await set_tenant_context(session, org_id)
        yield session
