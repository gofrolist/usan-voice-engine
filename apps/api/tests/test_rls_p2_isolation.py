"""Task D3: multi-org + act-as isolation suite.

End-to-end proof of the P2 tenancy guarantees built across Units B/C/D — no new
implementation, just the behaviours an auditor cares about:

* **Two-org admin-route isolation** — a profile created in org A is invisible to
  an admin whose active org is B, and vice-versa (RLS scopes the read to the
  principal's active org).
* **Act-as full-write** — a super-admin acting-as a non-member org can WRITE into
  that org's data; the row lands in the target org and the audit row records the
  super-admin's *real* email with ``acting_as`` semantics.
* **Instant revocation** — deleting a membership revokes access on the very next
  request (the session JWT is stateless; the membership read is the revocation
  seam), returning 403.
* **No active org → 409** — a super-admin with no active org hitting an org-scoped
  route gets 409 ("select an organization first").
* **Cross-org members API** — the active org comes from the principal, never the
  URL, so an admin can never see another org's members.

Most tests use ``isolation_client``: a *local* ``get_tenant_db`` override that
mirrors production — it opens a usan_app (RLS-subject) session, enforces the
409-on-no-active-org guard, and scopes the session to ``principal.active_org_id``
(a NullPool engine reused across the test's several requests, so it survives the
event loop), like test_admin_routes_org_scoped.py / test_admin_members.py. That
exercises the real per-principal scoping seam end-to-end.

The act-as full-write test instead uses the conftest's built-in override + the
``act_as_org`` helper (the seam Task D2 designed): that override's engine carries
the default-org connect baseline, which the create route's post-commit
``db.refresh`` needs to resolve the freshly-written row.
"""

import asyncio
import uuid

import pytest
from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import act_as_org
from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

# ---------------------------------------------------------------------------
# seeding helpers (superuser engine — the global control tables bypass RLS, and
# org-scoped seeds target a specific org regardless of any connect baseline)
# ---------------------------------------------------------------------------


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


async def _seed_profile_in_org(super_async_url: str, org_id: uuid.UUID, name: str) -> str:
    """Insert an agent_profile directly into ``org_id`` (bypassing RLS) and return its id.

    draft_config is JSONB NOT NULL with no server default — the row is created
    outside the Pydantic-validated route, so an empty object is sufficient for the
    isolation assertions (we never read the config back).
    """
    pid = str(uuid.uuid4())
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, organization_id, name, status, draft_config) "
                    "VALUES (CAST(:id AS uuid), :org, :n, 'active', CAST('{}' AS jsonb))"
                ),
                {"id": pid, "org": org_id, "n": name},
            )
    finally:
        await engine.dispose()
    return pid


async def _delete_profiles_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove every agent_profile (+ its versions) in the given orgs before teardown.

    The profiles FK the org, so they must go before ``two_orgs`` deletes the orgs.
    """
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM agent_profile_versions WHERE profile_id IN "
                    "(SELECT id FROM agent_profiles WHERE organization_id = ANY(:orgs))"
                ),
                {"orgs": list(org_ids)},
            )
            await conn.execute(
                text("DELETE FROM agent_profiles WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


async def _delete_audit_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove admin_audit_log rows in the given orgs before teardown.

    The act-as create writes a profile.create audit row into the target org; that row
    FKs the org (fk_admin_audit_log_organization), so it must go before ``two_orgs``
    deletes the org.
    """
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM admin_audit_log WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


async def _delete_membership(super_async_url: str, email: str, org_id: uuid.UUID) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM memberships WHERE email = :e AND organization_id = :o"),
                {"e": email.lower(), "o": org_id},
            )
    finally:
        await engine.dispose()


async def _audit_actions(super_async_url: str, actor: str) -> list[tuple[str, str | None, str]]:
    """All (action, entity_id, organization_id) audit rows for an actor (RLS bypassed)."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT action, entity_id, organization_id FROM admin_audit_log "
                        "WHERE actor_email = :a"
                    ),
                    {"a": actor.lower()},
                )
            ).all()
        return [(r[0], r[1], str(r[2])) for r in rows]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------


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


def _super_no_org_cookie(email: str) -> dict[str, str]:
    token = issue_session(
        email,
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


# ---------------------------------------------------------------------------
# fixture: a `client` whose get_tenant_db override mirrors PRODUCTION — it scopes
# the usan_app session to the principal's own active org and enforces the 409
# guard, so each request reads/writes exactly the principal's active org.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolation_client(client, app_async_database_url):
    tenant_engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async def _tenant_override(principal: AdminPrincipal = Depends(require_admin_session)):
        # Mirror the production get_tenant_db: 409 when no org is active, else open a
        # usan_app (RLS-subject) session scoped to the principal's active org.
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
    super_async_url = _super_url(app_async_database_url)
    try:
        yield client, super_async_url
    finally:
        client.app.dependency_overrides.pop(get_tenant_db, None)
        asyncio.run(tenant_engine.dispose())


# ---------------------------------------------------------------------------
# 1. two-org admin-route isolation
# ---------------------------------------------------------------------------


def test_profiles_are_isolated_between_orgs(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    pid_a = asyncio.run(_seed_profile_in_org(super_url, org_a, "Profile A"))
    pid_b = asyncio.run(_seed_profile_in_org(super_url, org_b, "Profile B"))
    asyncio.run(_seed_member(super_url, "multi@example.com", org_a, "admin"))
    asyncio.run(_seed_member(super_url, "multi@example.com", org_b, "admin"))
    try:
        in_a = client.get(
            "/v1/admin/profiles",
            cookies=_member_cookie("multi@example.com", org_a, AdminRole.ADMIN),
        )
        assert in_a.status_code == 200
        ids_a = {p["id"] for p in in_a.json()}
        assert pid_a in ids_a
        assert pid_b not in ids_a

        in_b = client.get(
            "/v1/admin/profiles",
            cookies=_member_cookie("multi@example.com", org_b, AdminRole.ADMIN),
        )
        assert in_b.status_code == 200
        ids_b = {p["id"] for p in in_b.json()}
        assert pid_b in ids_b
        assert pid_a not in ids_b
    finally:
        asyncio.run(_delete_profiles_in_orgs(super_url, org_a, org_b))


# ---------------------------------------------------------------------------
# 2. act-as full-write lands in the target org + audited under the real email
# ---------------------------------------------------------------------------


def test_act_as_full_write_lands_in_target_org_and_is_audited(
    client, two_orgs, app_async_database_url
):
    """A super-admin acting-as a non-member org writes into THAT org, audited by email.

    Uses the conftest's built-in get_tenant_db override (installed on the shared
    `client`) + the `act_as_org` helper — the seam Task D2 designed for exactly this.
    That override's engine carries the default-org connect baseline, so the create
    route's post-commit `db.refresh` resolves the row; `act_as_org` repoints the
    scoped active org, mirroring a super-admin switch-org act-as into a target org.
    """
    super_url = _super_url(app_async_database_url)
    org_a, org_b = two_orgs
    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    cookie = _act_as_cookie("staff@usan.com", org_b)
    try:
        # Scope the org-aware session to org B (the act-as target) and write into it.
        act_as_org(client.app, org_b)
        created = client.post("/v1/admin/profiles", json={"name": "Acted Profile"}, cookies=cookie)
        assert created.status_code == 201, created.text
        new_id = created.json()["id"]

        # The write is visible scoped to org B...
        in_b = client.get("/v1/admin/profiles", cookies=cookie)
        assert any(p["id"] == new_id for p in in_b.json())
        # ...and invisible scoped to org A (the write landed only in B's data).
        act_as_org(client.app, org_a)
        in_a = client.get("/v1/admin/profiles", cookies=_act_as_cookie("staff@usan.com", org_a))
        assert all(p["id"] != new_id for p in in_a.json())

        # The audit row records the super-admin's REAL email and lands in org B.
        audits = asyncio.run(_audit_actions(super_url, "staff@usan.com"))
        creates = [a for a in audits if a[0] == "profile.create" and a[1] == new_id]
        assert creates, f"no profile.create audit row for {new_id}: {audits}"
        assert creates[0][2] == str(org_b)
    finally:
        # Both FK the org, so they must go before two_orgs deletes org A/B.
        asyncio.run(_delete_audit_in_orgs(super_url, org_a, org_b))
        asyncio.run(_delete_profiles_in_orgs(super_url, org_a, org_b))


# ---------------------------------------------------------------------------
# 3. deleting a membership revokes access on the next request
# ---------------------------------------------------------------------------


def test_membership_deletion_revokes_on_next_request(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "revoke@example.com", org_a, "admin"))
    cookie = _member_cookie("revoke@example.com", org_a, AdminRole.ADMIN)

    # Member can read while the membership exists.
    assert client.get("/v1/admin/profiles", cookies=cookie).status_code == 200

    # Revoke the membership; the stale (unexpired) cookie must lose access at once.
    asyncio.run(_delete_membership(super_url, "revoke@example.com", org_a))
    revoked = client.get("/v1/admin/profiles", cookies=cookie)
    assert revoked.status_code == 403


# ---------------------------------------------------------------------------
# 4. super-admin with no active org → 409 on an org-scoped route
# ---------------------------------------------------------------------------


def test_super_admin_no_active_org_gets_409(isolation_client):
    client, super_url = isolation_client
    asyncio.run(_seed_super_admin(super_url, "noorg@usan.com"))
    r = client.get("/v1/admin/profiles", cookies=_super_no_org_cookie("noorg@usan.com"))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 5. cross-org members API is blocked (active org comes from the principal)
# ---------------------------------------------------------------------------


def test_members_api_cannot_see_other_orgs(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "owner@example.com", org_a, "admin"))
    asyncio.run(_seed_member(super_url, "stranger@example.com", org_b, "admin"))

    listing = client.get(
        "/v1/admin/members",
        cookies=_member_cookie("owner@example.com", org_a, AdminRole.ADMIN),
    )
    assert listing.status_code == 200
    emails = {m["email"] for m in listing.json()}
    assert "owner@example.com" in emails
    assert "stranger@example.com" not in emails
