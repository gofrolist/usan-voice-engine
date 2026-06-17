"""Task C1: the per-org members router (/v1/admin/members).

The active org is resolved from the session principal (get_tenant_db), never the
URL: an admin manages only their active org's members. ADMIN may list/add/role/
remove; VIEWER may read but not write (403); removing the last ADMIN of an org is
refused (409); members of another org are never visible.

Self-contained (the conftest-wide get_tenant_db override lands in Unit D / Task
D2). Here a local get_tenant_db override opens a usan_app (RLS-subject) session and
scopes it to the principal's active org, mirroring test_admin_routes_org_scoped.py
(NullPool engine reused across requests so it survives the test's multiple calls).
"""

import asyncio
import uuid

import pytest
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context


async def _seed_member(super_async_url: str, email: str, org_id: uuid.UUID, role: str) -> None:
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
                    "VALUES (:e, :o, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "o": org_id, "r": role},
            )
    finally:
        await engine.dispose()


def _cookie(email: str, org_id: uuid.UUID, role: AdminRole) -> dict[str, str]:
    token = issue_session(
        email,
        active_org_id=org_id,
        role=role,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


@pytest.fixture
def members_client(client, app_async_database_url):
    """`client` with a local get_tenant_db override (usan_app session scoped to the
    caller's active org), plus the superuser DSN for seeding into a specific org."""
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
    super_async_url = app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)
    try:
        yield client, super_async_url
    finally:
        client.app.dependency_overrides.pop(get_tenant_db, None)
        asyncio.run(tenant_engine.dispose())


def test_admin_lists_and_adds_members(members_client, admin_session):
    client, _ = members_client
    # The admin is itself an ADMIN of the active (usan) org.
    listing = client.get("/v1/admin/members")
    assert listing.status_code == 200
    assert "admin@example.com" in {m["email"] for m in listing.json()}

    add = client.post("/v1/admin/members", json={"email": "Bob@Example.com", "role": "viewer"})
    assert add.status_code == 201
    assert add.json()["email"] == "bob@example.com"
    assert add.json()["role"] == "viewer"
    assert "bob@example.com" in {m["email"] for m in client.get("/v1/admin/members").json()}


def test_admin_changes_role_and_removes(members_client, admin_session):
    client, _ = members_client
    client.post("/v1/admin/members", json={"email": "bob@example.com", "role": "viewer"})

    patched = client.patch("/v1/admin/members/bob@example.com", json={"role": "admin"})
    assert patched.status_code == 200
    assert patched.json()["role"] == "admin"

    removed = client.delete("/v1/admin/members/bob@example.com")
    assert removed.status_code == 204
    assert "bob@example.com" not in {m["email"] for m in client.get("/v1/admin/members").json()}


def test_viewer_cannot_write(members_client, async_database_url):
    client, super_async_url = members_client
    # Seed a viewer in the usan (default) org and authenticate as them.
    org_id = asyncio.run(_resolve_usan_org(super_async_url))
    asyncio.run(_seed_member(super_async_url, "viewer@example.com", org_id, "viewer"))
    cookies = _cookie("viewer@example.com", org_id, AdminRole.VIEWER)
    client.cookies.set(SESSION_COOKIE_NAME, cookies[SESSION_COOKIE_NAME])

    assert client.get("/v1/admin/members").status_code == 200
    assert (
        client.post("/v1/admin/members", json={"email": "x@y.com", "role": "admin"}).status_code
        == 403
    )
    assert client.patch("/v1/admin/members/x@y.com", json={"role": "admin"}).status_code == 403
    assert client.delete("/v1/admin/members/x@y.com").status_code == 403


def test_removing_last_admin_409(members_client, admin_session):
    client, _ = members_client
    # admin@example.com is the sole ADMIN of the active org.
    assert client.delete("/v1/admin/members/admin@example.com").status_code == 409
    assert (
        client.patch("/v1/admin/members/admin@example.com", json={"role": "viewer"}).status_code
        == 409
    )


def test_cannot_see_another_orgs_members(members_client, two_orgs, admin_session):
    client, super_async_url = members_client
    _, org_b = two_orgs
    # A member exists only in org B; the active org is usan, so they are invisible.
    asyncio.run(_seed_member(super_async_url, "other@example.com", org_b, "admin"))
    emails = {m["email"] for m in client.get("/v1/admin/members").json()}
    assert "other@example.com" not in emails


def test_add_invalid_email_422(members_client, admin_session):
    client, _ = members_client
    assert client.post("/v1/admin/members", json={"email": "not-an-email"}).status_code == 422


def test_requires_session(members_client):
    client, _ = members_client
    client.cookies.clear()
    assert client.get("/v1/admin/members").status_code == 401


async def _resolve_usan_org(super_async_url: str) -> uuid.UUID:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(text("SELECT id FROM organizations WHERE slug = 'usan'"))
            ).scalar_one()
    finally:
        await engine.dispose()
