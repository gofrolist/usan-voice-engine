"""Regression: the connect-event default-org baseline must survive pooled-connection reuse.

Background workers (retry/webhook/schedule/callback/family/retention pollers) open
sessions OUTSIDE get_db, so they never run get_db's per-request set_config — they rely
solely on the connect-time baseline set by _install_default_org_context. That baseline is
session-level set_config(is_local=false), which is STILL transactional: it must be
committed at connect, or the connection's first reset-on-return ROLLBACK (on pool
check-in) reverts app.current_org. Because the connect hook fires only once per physical
connection, a reused connection then runs context-free and every RLS-filtered worker query
fails with: invalid input syntax for type uuid: "" (the v0.6.0 prod incident).

The app's production engine pools connections, so reuse is the norm; the test fixtures use
NullPool (fresh connect every session), which masked this — hence a dedicated pooled test.
"""

import uuid as uuidlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.session import _install_default_org_context


async def test_connect_baseline_survives_pool_reuse(app_async_database_url: str) -> None:
    # pool_size=1 / max_overflow=0 forces the 2nd checkout to REUSE the 1st physical
    # connection, so the connect hook does NOT re-fire — exactly the prod worker scenario.
    engine = create_async_engine(app_async_database_url, pool_size=1, max_overflow=0)
    _install_default_org_context(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # First checkout: run + roll back a transaction (mirrors the pool check-in reset
        # that previously reverted the uncommitted baseline).
        async with factory() as s1:
            await s1.execute(text("SELECT 1"))
            await s1.rollback()
        # Second checkout reuses the same connection; the baseline must still be present.
        async with factory() as s2:
            current = await s2.scalar(text("SELECT current_setting('app.current_org', true)"))
            expected = await s2.scalar(text("SELECT default_org_id()::text"))
    finally:
        await engine.dispose()

    assert current, f"connect baseline lost after txn rollback on reused connection: {current!r}"
    assert current == expected
    uuidlib.UUID(current)  # well-formed uuid, never the '' that crashes ''::uuid under RLS
