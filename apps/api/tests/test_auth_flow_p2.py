"""Task B3: org-scoped login callback, extended /me, switch-org + act-as.

The callback resolves the active org from the caller's memberships (or denies a
0-membership non-super-admin); /me returns the org list + active org + flags;
switch-org re-issues the session for a membership org, or — for a super-admin —
acts-as a non-member org (with an audit row in the target org). These exercise
the wiring through routers/auth.py + schemas/auth.py with OAuth mocked.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import oauth
from usan_api.admin_session import (
    SESSION_COOKIE_NAME,
    TX_COOKIE_NAME,
    decode_session,
    issue_session,
    issue_tx,
)
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


# ---------------------------------------------------------------------------
# seeding helpers (superuser engine; the routes read the global control tables)
# ---------------------------------------------------------------------------
def _usan_org_id(async_database_url: str) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return (
                    await conn.execute(text("SELECT id FROM organizations WHERE slug='usan'"))
                ).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _make_org(async_database_url: str, slug: str) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return (
                    await conn.execute(
                        text("INSERT INTO organizations (name, slug) VALUES (:n, :s) RETURNING id"),
                        {"n": slug.upper(), "s": slug},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _seed_identity(async_database_url: str, email: str, *, is_super: bool = False) -> None:
    async def _run() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                        "VALUES (:e, :s, 'active', 'test') "
                        "ON CONFLICT (email) DO UPDATE SET is_super_admin = EXCLUDED.is_super_admin"
                    ),
                    {"e": email.lower(), "s": is_super},
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _add_membership(async_database_url: str, email: str, org_id: uuid.UUID, role: str) -> None:
    async def _run() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
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

    asyncio.run(_run())


def _audit_actions(async_database_url: str, actor: str) -> list[tuple[str, str | None]]:
    """All (action, entity_id) audit rows for an actor (superuser bypasses RLS)."""

    async def _run() -> list[tuple[str, str | None]]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text("SELECT action, entity_id FROM admin_audit_log WHERE actor_email=:a"),
                        {"a": actor.lower()},
                    )
                ).all()
            return [(r[0], r[1]) for r in rows]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _mock_oauth(monkeypatch, email: str) -> None:
    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": email, "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)


def _do_callback(sso_client, state: str = "S"):
    tx = issue_tx(state, "verifier", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    return sso_client.get("/v1/auth/callback", params={"code": "abc", "state": state})


# ---------------------------------------------------------------------------
# callback: active-org resolution
# ---------------------------------------------------------------------------
def test_callback_single_membership_logs_in_with_that_org(
    sso_client, async_database_url, monkeypatch
):
    org = _usan_org_id(async_database_url)
    _seed_identity(async_database_url, "one@x.com")
    _add_membership(async_database_url, "one@x.com", org, "viewer")
    _mock_oauth(monkeypatch, "one@x.com")

    r = _do_callback(sso_client)
    assert r.status_code == 303
    token = r.cookies[SESSION_COOKIE_NAME]
    claims = decode_session(token, get_settings())
    assert claims["active_org"] == str(org)
    assert claims["role"] == "viewer"
    assert claims["acting_as"] is False
    assert claims["super"] is False


def test_callback_zero_membership_non_super_is_denied(sso_client, async_database_url, monkeypatch):
    _seed_identity(async_database_url, "noaccess@x.com")
    _mock_oauth(monkeypatch, "noaccess@x.com")

    r = _do_callback(sso_client)
    assert r.status_code == 403
    assert ("auth.denied", "noaccess@x.com") in _audit_actions(async_database_url, "noaccess@x.com")


def test_callback_super_admin_zero_membership_logs_in_no_active_org(
    sso_client, async_database_url, monkeypatch
):
    _seed_identity(async_database_url, "boss@x.com", is_super=True)
    _mock_oauth(monkeypatch, "boss@x.com")

    r = _do_callback(sso_client)
    assert r.status_code == 303
    claims = decode_session(r.cookies[SESSION_COOKIE_NAME], get_settings())
    assert claims["active_org"] is None
    assert claims["role"] is None
    assert claims["super"] is True


def test_callback_unknown_identity_is_denied(sso_client, async_database_url, monkeypatch):
    _mock_oauth(monkeypatch, "ghost@x.com")
    r = _do_callback(sso_client)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /me — extended payload
# ---------------------------------------------------------------------------
def test_me_returns_orgs_active_and_flags(client, async_database_url):
    org = _usan_org_id(async_database_url)
    _seed_identity(async_database_url, "me@x.com")
    _add_membership(async_database_url, "me@x.com", org, "admin")
    token = issue_session(
        "me@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.get("/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "me@x.com"
    assert body["is_super_admin"] is False
    assert body["acting_as"] is False
    assert body["active_org"]["id"] == str(org)
    assert body["active_org"]["role"] == "admin"
    assert [o["id"] for o in body["orgs"]] == [str(org)]
    assert body["orgs"][0]["role"] == "admin"
    # Deployed build version for the admin-UI footer; "dev" in the uncontainerized test env.
    assert body["version"] == "dev"


def test_me_super_admin_no_active_org(client, async_database_url):
    _seed_identity(async_database_url, "boss2@x.com", is_super=True)
    token = issue_session(
        "boss2@x.com",
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.get("/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["is_super_admin"] is True
    assert body["active_org"] is None
    assert body["orgs"] == []


# ---------------------------------------------------------------------------
# switch-org
# ---------------------------------------------------------------------------
def test_switch_org_to_membership_org_succeeds(client, async_database_url):
    usan = _usan_org_id(async_database_url)
    other = _make_org(async_database_url, "switchteam")
    _seed_identity(async_database_url, "sw@x.com")
    _add_membership(async_database_url, "sw@x.com", usan, "admin")
    _add_membership(async_database_url, "sw@x.com", other, "viewer")
    token = issue_session(
        "sw@x.com",
        active_org_id=usan,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.post("/v1/auth/switch-org", json={"organization_id": str(other)})
    assert r.status_code == 200
    body = r.json()
    assert body["active_org"]["id"] == str(other)
    assert body["active_org"]["role"] == "viewer"
    assert body["acting_as"] is False
    claims = decode_session(r.cookies[SESSION_COOKIE_NAME], get_settings())
    assert claims["active_org"] == str(other)
    assert claims["role"] == "viewer"
    assert claims["acting_as"] is False


def test_switch_org_member_path_writes_switch_audit(client, async_database_url):
    """A plain member switch is audited too (not only act-as) — design §9. A bare org
    switch gates all subsequent data access, so it must leave a trail."""
    usan = _usan_org_id(async_database_url)
    other = _make_org(async_database_url, "auditteam")
    _seed_identity(async_database_url, "aud@x.com")
    _add_membership(async_database_url, "aud@x.com", usan, "admin")
    _add_membership(async_database_url, "aud@x.com", other, "viewer")
    token = issue_session(
        "aud@x.com",
        active_org_id=usan,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.post("/v1/auth/switch-org", json={"organization_id": str(other)})
    assert r.status_code == 200
    assert ("auth.switch_org", str(other)) in _audit_actions(async_database_url, "aud@x.com")


def test_switch_org_super_admin_to_non_member_is_act_as(client, async_database_url):
    other = _make_org(async_database_url, "actasteam")
    _seed_identity(async_database_url, "su@x.com", is_super=True)
    token = issue_session(
        "su@x.com",
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.post("/v1/auth/switch-org", json={"organization_id": str(other)})
    assert r.status_code == 200
    body = r.json()
    assert body["acting_as"] is True
    assert body["active_org"]["id"] == str(other)
    assert body["active_org"]["role"] is None
    claims = decode_session(r.cookies[SESSION_COOKIE_NAME], get_settings())
    assert claims["acting_as"] is True
    assert claims["active_org"] == str(other)
    # An act-as audit row lands under the real super-admin email, targeting the org.
    assert ("auth.act_as", str(other)) in _audit_actions(async_database_url, "su@x.com")


def test_switch_org_non_super_to_non_member_is_403(client, async_database_url):
    usan = _usan_org_id(async_database_url)
    other = _make_org(async_database_url, "forbidteam")
    _seed_identity(async_database_url, "deny@x.com")
    _add_membership(async_database_url, "deny@x.com", usan, "admin")
    token = issue_session(
        "deny@x.com",
        active_org_id=usan,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.post("/v1/auth/switch-org", json={"organization_id": str(other)})
    assert r.status_code == 403


def test_switch_org_unknown_org_is_404(client, async_database_url):
    usan = _usan_org_id(async_database_url)
    _seed_identity(async_database_url, "u404@x.com")
    _add_membership(async_database_url, "u404@x.com", usan, "admin")
    token = issue_session(
        "u404@x.com",
        active_org_id=usan,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = client.post("/v1/auth/switch-org", json={"organization_id": str(uuid.uuid4())})
    assert r.status_code == 404
