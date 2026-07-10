import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _app_url(async_database_url: str) -> str:
    # The app role (RLS-subject). Seeds use the superuser url; queries-under-test use this.
    return async_database_url.replace("usan:usan@", "usan_app:usan_app@", 1)


async def _seed_two_orgs(super_url: str) -> tuple[str, str, str, str]:
    """As superuser (bypasses RLS): orgs A and B + one contact each. Returns ids."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            a = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO organizations (name, slug) VALUES ('A', :s) RETURNING id"
                        ),
                        {"s": f"a-{uuid.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
            )
            b = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO organizations (name, slug) VALUES ('B', :s) RETURNING id"
                        ),
                        {"s": f"b-{uuid.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
            )
            ca, cb = str(uuid.uuid4()), str(uuid.uuid4())
            for cid, org in ((ca, a), (cb, b)):
                await conn.execute(
                    text(
                        "INSERT INTO contacts (id, name, phone_e164, timezone, organization_id) "
                        "VALUES (CAST(:id AS uuid), 'C', :p, 'America/New_York', CAST(:o AS uuid))"
                    ),
                    {
                        "id": cid,
                        "p": f"+1{uuid.uuid4().int % 9_000_000_000 + 1_000_000_000}",
                        "o": org,
                    },
                )
        return a, b, ca, cb
    finally:
        await engine.dispose()


def test_rls_blocks_cross_tenant_reads(async_database_url):
    async def run():
        org_a, _org_b, ca, cb = await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_org', :o, false)"),
                    {"o": org_a},
                )
                ids = {
                    str(r) for r in (await conn.execute(text("SELECT id FROM contacts"))).scalars()
                }
                # Only A's row visible, with no app-layer filter (RLS does the filtering).
                assert ca in ids
                assert cb not in ids
                got = (
                    await conn.execute(
                        text("SELECT id FROM contacts WHERE id = CAST(:id AS uuid)"), {"id": cb}
                    )
                ).first()
                assert got is None
        finally:
            await app.dispose()

    asyncio.run(run())


def test_rls_fails_closed_without_context(async_database_url):
    async def run():
        await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                rows = (await conn.execute(text("SELECT id FROM contacts"))).scalars().all()
                assert rows == []  # no context => zero rows
        finally:
            await app.dispose()

    asyncio.run(run())


def test_rls_with_check_blocks_wrong_org_insert(async_database_url):
    async def run():
        org_a, org_b, _, _ = await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_org', :o, false)"),
                    {"o": org_a},
                )
                # RLS WITH CHECK rejects an insert whose organization_id != the context
                # org. asyncpg surfaces the policy violation as a DBAPIError.
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text(
                            "INSERT INTO contacts "
                            "(id, name, phone_e164, timezone, organization_id) "
                            "VALUES (gen_random_uuid(), 'X', '+15550000000', 'America/New_York', "
                            "CAST(:o AS uuid))"
                        ),
                        {"o": org_b},
                    )
        finally:
            await app.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "table",
    [
        "contacts",
        "dnc_list",
        "calls",
        "transcripts",
        "wellness_logs",
        "medication_logs",
        "medication_reminders",
        "personal_facts",
        "conversation_summaries",
        "wellbeing_survey_results",
        "activity_history",
        "turn_metrics",
        "call_metrics",
        "agent_profiles",
        "agent_profile_versions",
        "call_schedules",
        "call_batches",
        "call_batch_targets",
        "webhook_endpoints",
        "webhook_deliveries",
        "custom_variables",
        "admin_audit_log",
        "follow_up_flags",
        "callback_requests",
        "sms_messages",
        "family_contacts",
        "family_tasks",
        "family_reports",
    ],
)
def test_every_tenant_table_is_rls_enabled_and_fails_closed(async_database_url, table):
    async def run():
        eng = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with eng.connect() as conn:
                rows = (await conn.execute(text(f"SELECT 1 FROM {table}"))).all()
                assert rows == []  # no context => fail closed
                meta = (
                    await conn.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = :t"
                        ),
                        {"t": table},
                    )
                ).one()
                assert meta[0] is True
                assert meta[1] is True
        finally:
            await eng.dispose()

    asyncio.run(run())


def test_app_engine_connections_inherit_default_org_context(async_database_url):
    """Background workers connect via the app engine (get_engine) OUTSIDE get_db (schedule
    orchestrator, webhook/sms/family jobs, retention, callback dialer). get_engine installs
    a connect-event listener (_install_default_org_context) that puts every connection into
    default-org context, so those worker sessions are not fail-closed under RLS. Verified by
    installing that SAME listener on a fresh usan_app engine — a bare usan_app engine (as the
    fail-closed tests above use) has NO context, so the listener is what provides it here."""
    from usan_api.db.session import _install_default_org_context

    async def run() -> str:
        eng = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        _install_default_org_context(eng)
        try:
            async with eng.connect() as conn:
                ctx = (
                    await conn.execute(text("SELECT current_setting('app.current_org', true)"))
                ).scalar_one()
                # Not fail-closed: a tenant table is queryable under the inherited context.
                await conn.execute(text("SELECT 1 FROM contacts"))
                return ctx
        finally:
            await eng.dispose()

    assert asyncio.run(run())  # a uuid string (the default org), not empty/None
