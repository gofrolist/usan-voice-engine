"""Task E1: cross-org invitation isolation & RBAC proof (under ``usan_app``).

The bulletproof-isolation proof for P3 invites: it runs the invite-management
endpoints under the non-superuser ``usan_app`` role with the production org seam
(``get_tenant_db`` scoped to ``principal.active_org_id``), proving an org cannot
see or accept another org's invites. It mirrors the ``isolation_client`` fixture
plus the ``_member_cookie``/``_act_as_cookie`` helpers from
``test_rls_p2_isolation.py`` (a local copy of the fixture/helpers lives here).
"""

import asyncio

import pytest
from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context


def _member_cookie(email, org_id, role):
    return {
        SESSION_COOKIE_NAME: issue_session(
            email,
            active_org_id=org_id,
            role=role,
            is_super_admin=False,
            acting_as=False,
            settings=get_settings(),
        )
    }


def _act_as_cookie(email, org_id):
    return {
        SESSION_COOKIE_NAME: issue_session(
            email,
            active_org_id=org_id,
            role=AdminRole.ADMIN,
            is_super_admin=True,
            acting_as=True,
            settings=get_settings(),
        )
    }


def _super_url(app_async_database_url: str) -> str:
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


async def _seed_member(super_url, email, org_id, role):
    eng = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, false, 'active', 'test') ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "VALUES (:e, :o, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email, "o": org_id, "r": role},
            )
    finally:
        await eng.dispose()


async def _seed_super_admin(super_url, email):
    """An active super-admin identity (no membership). require_admin_session reads
    admin_users every request, so an act-as / no-active-org super-admin session is
    only honoured when this row exists and is_super_admin."""
    eng = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, true, 'active', 'test') "
                    "ON CONFLICT (email) DO UPDATE SET is_super_admin = true, status = 'active'"
                ),
                {"e": email},
            )
    finally:
        await eng.dispose()


async def _delete_audit_in_orgs(super_url, *org_ids):
    """Drop admin_audit_log rows in the given orgs before two_orgs teardown — the
    invite.* audit rows FK the org (fk_admin_audit_log_organization)."""
    eng = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text("DELETE FROM admin_audit_log WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await eng.dispose()


@pytest.fixture
def isolation_client(client, app_async_database_url):
    tenant_engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async def _tenant_override(principal: AdminPrincipal = Depends(require_admin_session)):
        if principal.active_org_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "select an organization first")
        async with tenant_factory() as session:
            await set_tenant_context(session, principal.active_org_id)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    client.app.dependency_overrides[get_tenant_db] = _tenant_override
    try:
        yield client, _super_url(app_async_database_url)
    finally:
        client.app.dependency_overrides.pop(get_tenant_db, None)
        asyncio.run(tenant_engine.dispose())


def test_invites_isolated_between_orgs(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "a-admin@x.com", org_a, "admin"))
    asyncio.run(_seed_member(super_url, "b-admin@x.com", org_b, "admin"))
    try:
        created = client.post(
            "/v1/admin/invites",
            json={"email": "guest@x.com", "role": "viewer"},
            cookies=_member_cookie("a-admin@x.com", org_a, AdminRole.ADMIN),
        )
        assert created.status_code == 201
        iid = created.json()["id"]

        in_b = client.get(
            "/v1/admin/invites", cookies=_member_cookie("b-admin@x.com", org_b, AdminRole.ADMIN)
        )
        assert in_b.status_code == 200
        assert in_b.json() == []

        rev_b = client.delete(
            f"/v1/admin/invites/{iid}",
            cookies=_member_cookie("b-admin@x.com", org_b, AdminRole.ADMIN),
        )
        assert rev_b.status_code == 404

        in_a = client.get(
            "/v1/admin/invites", cookies=_member_cookie("a-admin@x.com", org_a, AdminRole.ADMIN)
        )
        assert [i["email"] for i in in_a.json()] == ["guest@x.com"]
    finally:
        # invite.create audit row FKs the org — drop it before two_orgs teardown.
        asyncio.run(_delete_audit_in_orgs(super_url, org_a, org_b))


def test_super_admin_act_as_can_invite(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, _ = two_orgs
    # require_admin_session re-reads admin_users every request; the act-as session is
    # only honoured for an active super-admin identity.
    asyncio.run(_seed_super_admin(super_url, "super@x.com"))
    try:
        r = client.post(
            "/v1/admin/invites",
            json={"email": "viaact@x.com", "role": "viewer"},
            cookies=_act_as_cookie("super@x.com", org_a),
        )
        assert r.status_code == 201, r.text
    finally:
        asyncio.run(_delete_audit_in_orgs(super_url, org_a))


def test_no_active_org_409(isolation_client):
    client, super_url = isolation_client
    asyncio.run(_seed_super_admin(super_url, "super@x.com"))
    token = issue_session(
        "super@x.com",
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
        settings=get_settings(),
    )
    r = client.get("/v1/admin/invites", cookies={SESSION_COOKIE_NAME: token})
    assert r.status_code == 409
