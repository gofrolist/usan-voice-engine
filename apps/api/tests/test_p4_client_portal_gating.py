"""P4: client-portal gating + operator-surface isolation.

Proves the P4 admin-plane guarantees:
- Operator-only routers (profiles, defaults, custom-variables, the profile-authoring
  catalogs) require a super-admin (USAN operator): a client-org ADMIN gets 403, a
  super-admin (acting-as) gets 200.
- The audit log is ADMIN-gated: a client VIEWER gets 403, a client ADMIN gets 200
  (org-scoped by the same RLS seam proven in test_rls_p2_isolation).

Helpers mirror test_rls_p2_isolation (superuser-engine seeding + session cookies).
The 403 assertions hit the router-level gate before get_tenant_db, so the shared
`client` fixture suffices; the 200 assertions scope the shared get_tenant_db override
to the principal's org via conftest's `act_as_org`.
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

# GET endpoints that become operator-only (super-admin) in P4.
OPERATOR_GET_ENDPOINTS = [
    "/v1/admin/profiles",
    "/v1/admin/defaults",
    "/v1/admin/custom-variables",
    "/v1/admin/voice-catalog",
    "/v1/admin/model-catalog",
    "/v1/admin/tool-catalog",
    "/v1/admin/variable-catalog",
]


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


@pytest.mark.parametrize("path", OPERATOR_GET_ENDPOINTS)
def test_client_admin_forbidden_on_operator_endpoints(
    client, two_orgs, app_async_database_url, path
):
    super_url = _super_url(app_async_database_url)
    _, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "client@example.com", org_b, "admin"))
    # 403 fires at the router-level require_super_admin gate, before get_tenant_db.
    r = client.get(path, cookies=_member_cookie("client@example.com", org_b, AdminRole.ADMIN))
    assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", OPERATOR_GET_ENDPOINTS)
def test_super_admin_allowed_on_operator_endpoints(client, two_orgs, app_async_database_url, path):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    # Scope the shared get_tenant_db override to the act-as target org.
    act_as_org(client.app, org_a)
    r = client.get(path, cookies=_act_as_cookie("staff@usan.com", org_a))
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


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
