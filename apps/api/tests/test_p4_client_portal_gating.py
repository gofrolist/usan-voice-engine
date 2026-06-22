"""Self-service authoring gating + tenant isolation (org-admin profile authoring).

Proves the admin-plane authz for the profile-authoring surface after it was opened
from super-admin-only to org-scoped self-service:

- The authoring READ endpoints (profiles, defaults, custom-variables, and the
  voice/model/tool/variable catalogs) are readable by any member of the active org —
  a client ADMIN, a client VIEWER, and a super-admin acting-as all get 200.
- The authoring WRITE endpoints are ADMIN-gated by the per-endpoint
  ``require_admin_role(ADMIN)``: a client VIEWER gets 403, a client ADMIN gets 201.
- RLS scopes every write/read to the caller's active org, so org A never sees org B's
  authoring rows (cross-tenant isolation proof, mirroring test_rls_p2_isolation).
- The audit log + invites stay ADMIN-gated (a client VIEWER gets 403), unchanged.

Helpers mirror test_rls_p2_isolation (superuser-engine seeding + session cookies).
Reads/writes that must land in a specific org scope the shared get_tenant_db override
via conftest's ``act_as_org``.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import act_as_org
from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

# GET endpoints readable by any member of the active org (a client ADMIN OR VIEWER).
AUTHORING_GET_ENDPOINTS = [
    "/v1/admin/profiles",
    "/v1/admin/defaults",
    "/v1/admin/custom-variables",
    "/v1/admin/voice-catalog",
    "/v1/admin/model-catalog",
    "/v1/admin/tool-catalog",
    "/v1/admin/variable-catalog",
]

_CUSTOM_VARS = "/v1/admin/custom-variables"


def _super_url(app_async_database_url: str) -> str:
    """The superuser (RLS-bypassing) async DSN, derived from the usan_app one."""
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


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


async def _seed_super_admin(super_async_url: str, email: str) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, true, 'active', 'test') "
                    "ON CONFLICT (email) DO UPDATE SET is_super_admin = true"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


async def _seed_audit_row(
    super_async_url: str, org_id: uuid.UUID, actor: str, entity_id: str
) -> None:
    """Insert one admin_audit_log row into ``org_id`` (RLS-bypassing superuser engine).

    ``entity_id`` is the per-org marker the test reads back to prove org-scoping.
    """
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_audit_log "
                    "(organization_id, actor_email, action, entity_type, entity_id) "
                    "VALUES (:o, :a, 'profile.create', 'profile', :eid)"
                ),
                {"o": org_id, "a": actor.lower(), "eid": entity_id},
            )
    finally:
        await engine.dispose()


async def _delete_audit_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove admin_audit_log rows in the given orgs before teardown (they FK the org)."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM admin_audit_log WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


async def _delete_custom_vars_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove custom_variables rows in the given orgs before teardown (they FK the org)."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM custom_variables WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


async def _delete_profiles_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove agent_profiles (versions cascade) in the given orgs before teardown."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_profiles WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


def _member_cookie(email: str, org_id: uuid.UUID, role: AdminRole) -> dict[str, str]:
    token = issue_session(
        email,
        active_org_id=org_id,
        role=role,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def _act_as_cookie(email: str, org_id: uuid.UUID) -> dict[str, str]:
    """A super-admin session acting-as ``org_id`` (no membership there)."""
    token = issue_session(
        email,
        active_org_id=org_id,
        role=AdminRole.ADMIN,
        is_super_admin=True,
        acting_as=True,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def _create_var(client, name: str, cookies: dict[str, str]):
    body = {"name": name, "description": "", "example": "", "phi": False}
    return client.post(_CUSTOM_VARS, json=body, cookies=cookies)


# --- unauthenticated callers are rejected (defense-in-depth on the relaxed routers) ---
@pytest.mark.parametrize("path", AUTHORING_GET_ENDPOINTS)
def test_unauthenticated_request_is_401(client, path):
    # require_admin_role(VIEWER) pulls require_admin_session, so a request with no session
    # cookie is still rejected — the routers are member-readable, never public.
    client.cookies.clear()
    r = client.get(path)
    assert r.status_code == 401, f"{path}: {r.status_code} {r.text}"


# --- authoring READS: any member of the active org (admin, viewer, super acting-as) ---
@pytest.mark.parametrize("path", AUTHORING_GET_ENDPOINTS)
def test_client_admin_can_read_authoring_endpoints(client, two_orgs, app_async_database_url, path):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "admin@example.com", org_a, "admin"))
    act_as_org(client.app, org_a)
    r = client.get(path, cookies=_member_cookie("admin@example.com", org_a, AdminRole.ADMIN))
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", AUTHORING_GET_ENDPOINTS)
def test_client_viewer_can_read_authoring_endpoints(client, two_orgs, app_async_database_url, path):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "viewer@example.com", org_a, "viewer"))
    act_as_org(client.app, org_a)
    r = client.get(path, cookies=_member_cookie("viewer@example.com", org_a, AdminRole.VIEWER))
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", AUTHORING_GET_ENDPOINTS)
def test_super_admin_can_read_authoring_endpoints(client, two_orgs, app_async_database_url, path):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    act_as_org(client.app, org_a)
    r = client.get(path, cookies=_act_as_cookie("staff@usan.com", org_a))
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


# --- authoring WRITES: ADMIN only; a client VIEWER is forbidden ---
def test_client_viewer_forbidden_on_authoring_write(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "viewer@example.com", org_a, "viewer"))
    act_as_org(client.app, org_a)
    r = _create_var(
        client, "pet_name", _member_cookie("viewer@example.com", org_a, AdminRole.VIEWER)
    )
    assert r.status_code == 403, r.text


def test_client_admin_can_write_authoring(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "admin@example.com", org_a, "admin"))
    act_as_org(client.app, org_a)
    try:
        r = _create_var(
            client, "pet_name", _member_cookie("admin@example.com", org_a, AdminRole.ADMIN)
        )
        assert r.status_code == 201, r.text
    finally:
        # The create is audited, so clear both the var and its audit row before two_orgs
        # tears the org down (admin_audit_log FKs the org).
        asyncio.run(_delete_custom_vars_in_orgs(super_url, org_a))
        asyncio.run(_delete_audit_in_orgs(super_url, org_a))


def test_client_admin_can_author_a_profile(client, two_orgs, app_async_database_url):
    """The headline path: a plain org ADMIN creates AND publishes a profile in their own org."""
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "admin@example.com", org_a, "admin"))
    act_as_org(client.app, org_a)
    cookies = _member_cookie("admin@example.com", org_a, AdminRole.ADMIN)
    try:
        created = client.post("/v1/admin/profiles", json={"name": "Org A Greeter"}, cookies=cookies)
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        published = client.post(
            f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"}, cookies=cookies
        )
        assert published.status_code == 201, published.text
    finally:
        asyncio.run(_delete_profiles_in_orgs(super_url, org_a))
        asyncio.run(_delete_audit_in_orgs(super_url, org_a))


# --- cross-tenant isolation: an org-A write is invisible to an org-B admin (RLS) ---
def test_authoring_writes_are_org_scoped(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "client@example.com", org_a, "admin"))
    asyncio.run(_seed_member(super_url, "client@example.com", org_b, "admin"))
    try:
        # Each org's admin writes one variable into their OWN org.
        act_as_org(client.app, org_a)
        a = _create_var(
            client, "alpha_in_a", _member_cookie("client@example.com", org_a, AdminRole.ADMIN)
        )
        assert a.status_code == 201, a.text
        act_as_org(client.app, org_b)
        b = _create_var(
            client, "beta_in_b", _member_cookie("client@example.com", org_b, AdminRole.ADMIN)
        )
        assert b.status_code == 201, b.text

        # Reading as the org-B admin returns ONLY org B's row — org A's is RLS-isolated.
        r = client.get(
            _CUSTOM_VARS, cookies=_member_cookie("client@example.com", org_b, AdminRole.ADMIN)
        )
        assert r.status_code == 200, r.text
        names = {row["name"] for row in r.json()}
        assert "beta_in_b" in names  # org B's own row is visible
        assert "alpha_in_a" not in names  # org A's row is invisible across the tenant boundary
    finally:
        # Both creates are audited — clear vars + audit rows before two_orgs teardown.
        asyncio.run(_delete_custom_vars_in_orgs(super_url, org_a, org_b))
        asyncio.run(_delete_audit_in_orgs(super_url, org_a, org_b))


# --- audit + invites stay ADMIN-gated (unchanged) ---
def test_client_viewer_forbidden_on_audit(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "viewer@example.com", org_a, "viewer"))
    act_as_org(client.app, org_a)
    r = client.get(
        "/v1/admin/audit",
        cookies=_member_cookie("viewer@example.com", org_a, AdminRole.VIEWER),
    )
    assert r.status_code == 403, r.text


def test_client_admin_allowed_on_own_audit(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "auditor@example.com", org_a, "admin"))
    act_as_org(client.app, org_a)
    r = client.get(
        "/v1/admin/audit",
        cookies=_member_cookie("auditor@example.com", org_a, AdminRole.ADMIN),
    )
    assert r.status_code == 200, r.text


def test_audit_is_org_scoped_for_client_admin(client, two_orgs, app_async_database_url):
    """§5.3 RLS isolation proof: a client ADMIN's audit list returns only its org's rows.

    Seed one audit row into org A and one into org B (RLS-bypassing superuser engine),
    then read GET /v1/admin/audit as an org-B ADMIN scoped to org B via get_tenant_db.
    The response must contain org B's row and must NOT contain org A's row.
    """
    super_url = _super_url(app_async_database_url)
    org_a, org_b = two_orgs
    entity_a = f"audit-a-{uuid.uuid4().hex[:8]}"
    entity_b = f"audit-b-{uuid.uuid4().hex[:8]}"
    asyncio.run(_seed_audit_row(super_url, org_a, "staff@usan.com", entity_a))
    asyncio.run(_seed_audit_row(super_url, org_b, "client@example.com", entity_b))
    asyncio.run(_seed_member(super_url, "client@example.com", org_b, "admin"))
    try:
        act_as_org(client.app, org_b)
        r = client.get(
            "/v1/admin/audit",
            cookies=_member_cookie("client@example.com", org_b, AdminRole.ADMIN),
        )
        assert r.status_code == 200, r.text
        entity_ids = {row["entity_id"] for row in r.json()}
        assert entity_b in entity_ids  # org B's own row is visible
        assert entity_a not in entity_ids  # org A's row is RLS-isolated
    finally:
        asyncio.run(_delete_audit_in_orgs(super_url, org_a, org_b))


def test_client_viewer_forbidden_on_invites(client, two_orgs, app_async_database_url):
    """GET /v1/admin/invites is ADMIN-gated: a client VIEWER gets 403 at the router gate."""
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "viewer@example.com", org_a, "viewer"))
    act_as_org(client.app, org_a)
    r = client.get(
        "/v1/admin/invites",
        cookies=_member_cookie("viewer@example.com", org_a, AdminRole.VIEWER),
    )
    assert r.status_code == 403, r.text
