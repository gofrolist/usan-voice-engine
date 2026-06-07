import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

# Migrations (incl. 0009) run once via the session-scoped `database_url`
# fixture's `alembic upgrade head`. Depending on `async_database_url` guarantees
# they have been applied.


async def test_grafana_ro_exists_and_can_log_in(async_database_url):
    engine = create_async_engine(async_database_url)
    try:
        async with engine.connect() as conn:
            rolcanlogin = await conn.scalar(
                sa.text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'grafana_ro'")
            )
        assert rolcanlogin is True
    finally:
        await engine.dispose()


async def test_grafana_ro_can_select_reporting_tables(async_database_url):
    engine = create_async_engine(async_database_url)
    allowed = (
        "calls",
        "elders",
        "wellness_logs",
        "medication_logs",
        "turn_metrics",
        "call_metrics",
    )
    try:
        async with engine.connect() as conn:
            for table in allowed:
                granted = await conn.scalar(
                    sa.text("SELECT has_table_privilege('grafana_ro', :t, 'SELECT')").bindparams(
                        t=table
                    )
                )
                assert granted is True, f"grafana_ro should SELECT {table}"
    finally:
        await engine.dispose()


async def test_grafana_ro_cannot_read_excluded_phi_tables(async_database_url):
    # transcripts (raw conversation PHI) and dnc_list (phone-number PII) are
    # deliberately NOT granted; the role must not be able to read them.
    engine = create_async_engine(async_database_url)
    excluded = ("transcripts", "dnc_list")
    try:
        async with engine.connect() as conn:
            for table in excluded:
                granted = await conn.scalar(
                    sa.text("SELECT has_table_privilege('grafana_ro', :t, 'SELECT')").bindparams(
                        t=table
                    )
                )
                assert granted is False, f"grafana_ro must NOT read {table}"
    finally:
        await engine.dispose()


async def test_grafana_ro_is_read_only(async_database_url):
    # No write privilege (INSERT/UPDATE/DELETE) on a granted table.
    engine = create_async_engine(async_database_url)
    try:
        async with engine.connect() as conn:
            for priv in ("INSERT", "UPDATE", "DELETE"):
                granted = await conn.scalar(
                    sa.text("SELECT has_table_privilege('grafana_ro', 'calls', :p)").bindparams(
                        p=priv
                    )
                )
                assert granted is False, f"grafana_ro must not have {priv} on calls"
    finally:
        await engine.dispose()
