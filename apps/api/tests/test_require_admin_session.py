import asyncio
import datetime

import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


async def _seed(async_database_url: str, email: str) -> None:
    """Seed an identity-only admin_users row (role now rides the session claims, P2)."""
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, status, added_by) "
                    "VALUES (:e, 'active', 'test') "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def test_no_cookie_is_401(client, admin_session):
    # Probe a require_admin_session route (contacts) — profiles is super-admin-only in
    # P4 and would 403 for the wrong reason; contacts isolates the 401 (no-cookie) path.
    client.cookies.clear()
    r = client.get("/v1/admin/contacts")
    assert r.status_code == 401


def test_valid_session_authenticates(client, admin_session):
    # Uses a non-operator admin route (P4 made /v1/admin/profiles super-admin only);
    # contacts stays require_admin_session, so it proves a valid session authenticates.
    r = client.get("/v1/admin/contacts")
    assert r.status_code == 200


def test_revoked_user_is_401(client, admin_session, async_database_url):
    async def _remove():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM admin_users WHERE email='admin@example.com'"))
        finally:
            await engine.dispose()

    asyncio.run(_remove())
    # Probe a require_admin_session route (contacts) — profiles is super-admin-only in
    # P4 and a revoked session there can 403 for the wrong reason; contacts isolates the
    # revocation 401 path.
    r = client.get("/v1/admin/contacts")
    assert r.status_code == 401


def test_role_gate_blocks_viewer(client, async_database_url):
    asyncio.run(_seed(async_database_url, "viewer@example.com"))
    token = issue_session(
        "viewer@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    # Exercise the role gate on a non-operator route (P4 made profiles super-admin only):
    # contacts is require_admin_session with ADMIN-gated writes, so a viewer reads but
    # cannot write. The route-level require_admin_role(ADMIN) 403s before the handler, so
    # the missing contact never reaches a 404.
    import uuid

    assert client.get("/v1/admin/contacts").status_code == 200  # viewer can read
    assert (
        client.put(
            f"/v1/admin/contacts/{uuid.uuid4()}/profile",
            json={"agent_profile_id": None},
        ).status_code
        == 403
    )  # not write


def test_invalid_signature_cookie_is_401(client, admin_session):
    # A session cookie signed with a different key must be rejected at the endpoint
    # (mapped to 401 by the dependency, not surfaced as a 500).
    exp = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    bad = jwt.encode(
        {"sub": "admin@example.com", "role": "admin", "typ": "admin_session", "exp": exp},
        "x" * 32,
        algorithm="HS256",
    )
    client.cookies.set(SESSION_COOKIE_NAME, bad)
    # Probe a require_admin_session route (contacts) — profiles is super-admin-only in
    # P4; contacts isolates the invalid-signature 401 path.
    assert client.get("/v1/admin/contacts").status_code == 401
