"""Task B4: admin routers resolve their org from the session via get_tenant_db.

A contact seeded into org A is visible to an admin whose active org is A and
invisible to one whose active org is B — RLS scopes the read to the principal's
active org, not the connect-baseline default org.

Self-contained (the conftest-wide get_tenant_db override + act_as helper land in
Unit D / Task D2). Here a local get_tenant_db override opens a usan_app
(RLS-subject) session and scopes it to the principal's active org, mirroring the
`client` fixture's own get_db override (NullPool engine reused across requests).
That exercises the swap under test end-to-end without binding the process-global
engine across this test's two requests (which would raise "Event loop is closed").
"""

import asyncio
import uuid

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context


async def _seed_member(super_async_url: str, email: str, org_id: uuid.UUID) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, status, added_by) "
                    "VALUES (:e, 'active', 'test') ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "VALUES (:e, :o, CAST('admin' AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO NOTHING"
                ),
                {"e": email.lower(), "o": org_id},
            )
    finally:
        await engine.dispose()


async def _seed_contact_in_org(
    super_async_url: str, org_id: uuid.UUID, name: str, phone: str
) -> str:
    cid = str(uuid.uuid4())
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO contacts (id, organization_id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :org, :n, :p, 'America/New_York')"
                ),
                {"id": cid, "org": org_id, "n": name, "p": phone},
            )
    finally:
        await engine.dispose()
    return cid


async def _delete_contact(super_async_url: str, contact_id: str) -> None:
    # The contact FKs the org, so it must go before `two_orgs` deletes org A.
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM contacts WHERE id = CAST(:id AS uuid)"), {"id": contact_id}
            )
    finally:
        await engine.dispose()


def _cookie(email: str, org_id: uuid.UUID) -> dict[str, str]:
    token = issue_session(
        email,
        active_org_id=org_id,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def test_admin_contacts_scoped_to_active_org(client, two_orgs, app_async_database_url):
    """An admin's contact list is scoped to the active org carried in their session."""
    # The superuser DSN (bypasses RLS) for seeding into a specific org.
    super_async_url = app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)
    org_a, org_b = two_orgs
    cid = asyncio.run(_seed_contact_in_org(super_async_url, org_a, "Org A Contact", "+15557770001"))
    asyncio.run(_seed_member(super_async_url, "scoped@example.com", org_a))
    asyncio.run(_seed_member(super_async_url, "scoped@example.com", org_b))

    # Local get_tenant_db override: a usan_app (RLS-subject) session scoped to the
    # principal's own active org. Same NullPool-engine-reused-across-requests shape
    # as the `client` fixture's get_db override, so it survives both requests.
    tenant_engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async def _tenant_override(principal: AdminPrincipal = Depends(require_admin_session)):
        async with tenant_factory() as session:
            await set_tenant_context(session, principal.active_org_id)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    client.app.dependency_overrides[get_tenant_db] = _tenant_override
    try:
        # Active org B: the org-A contact is invisible.
        in_b = client.get("/v1/admin/contacts", cookies=_cookie("scoped@example.com", org_b))
        assert in_b.status_code == 200
        assert all(row["id"] != cid for row in in_b.json())

        # Active org A: the same contact is returned.
        in_a = client.get("/v1/admin/contacts", cookies=_cookie("scoped@example.com", org_a))
        assert in_a.status_code == 200
        assert any(row["id"] == cid for row in in_a.json())
    finally:
        client.app.dependency_overrides.pop(get_tenant_db, None)
        asyncio.run(_delete_contact(super_async_url, cid))
        asyncio.run(tenant_engine.dispose())
