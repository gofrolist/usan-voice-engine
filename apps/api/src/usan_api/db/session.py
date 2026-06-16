from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from usan_api.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
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
