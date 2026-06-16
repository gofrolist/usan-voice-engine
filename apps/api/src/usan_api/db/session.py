from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from usan_api.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def _install_default_org_context(engine: AsyncEngine) -> None:
    """Register a connect-event listener that puts every connection from ``engine`` into
    the default-org tenant context.

    Background workers (schedule orchestrator, webhook/sms/family jobs, retention,
    callback dialer) open sessions OUTSIDE get_db, so they never run get_db's per-request
    set_config. In prod the app connects as the non-superuser usan_app role (RLS-subject);
    without a baseline context those worker sessions would see zero rows (fail closed) and
    every background job would silently no-op. This listener sets the baseline at connect.

    Scoped to the app engine ONLY (NOT a role-level GUC): connections from other engines —
    e.g. the RLS isolation tests' own create_async_engine() — stay context-free, so the
    spec's fail-closed-when-unset property (§3.3) remains testable and true. get_db still
    sets per-request context with is_local=true (the P2 seam where a real per-user org
    overrides this baseline); that reverts at txn end to this connect default.
    default_org_id() is the SQL function from migration 0031.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_default_org(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT set_config('app.current_org', default_org_id()::text, false)")
        finally:
            cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
        _install_default_org_context(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Sets the tenant context (P1: the default org), then yields.

    Handlers commit explicitly; this only rolls back and closes. Context is set inside
    the session's transaction (set_config is_local=true) so RLS sees it and it's cleared
    when the transaction ends — no leak across pooled connections.
    """
    from usan_api.tenant_context import resolve_default_org_id, set_tenant_context

    async with get_session_factory()() as session:
        try:
            org_id = await resolve_default_org_id(session)
            await set_tenant_context(session, org_id)
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _factory = None
