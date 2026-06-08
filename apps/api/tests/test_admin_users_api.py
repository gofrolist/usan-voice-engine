import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


async def _seed(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "r": role},
            )
    finally:
        await engine.dispose()


def test_list_includes_self(client, admin_session):
    r = client.get("/v1/admin/admin-users")
    assert r.status_code == 200
    emails = {u["email"] for u in r.json()}
    assert "admin@example.com" in emails


def test_add_then_remove_admin_user(client, admin_session):
    add = client.post("/v1/admin/admin-users", json={"email": "Bob@Example.com", "role": "viewer"})
    assert add.status_code == 201
    assert add.json()["email"] == "bob@example.com"
    assert add.json()["role"] == "viewer"

    emails = {u["email"] for u in client.get("/v1/admin/admin-users").json()}
    assert "bob@example.com" in emails

    rm = client.delete("/v1/admin/admin-users/bob@example.com")
    assert rm.status_code == 204
    emails = {u["email"] for u in client.get("/v1/admin/admin-users").json()}
    assert "bob@example.com" not in emails


def test_remove_unknown_returns_404(client, admin_session):
    r = client.delete("/v1/admin/admin-users/nobody@example.com")
    assert r.status_code == 404


def test_add_requires_session(client):
    r = client.post("/v1/admin/admin-users", json={"email": "x@y.com"})
    assert r.status_code == 401


def test_add_invalid_email_422(client, admin_session):
    r = client.post("/v1/admin/admin-users", json={"email": "not-an-email"})
    assert r.status_code == 422


def test_viewer_cannot_manage_admin_users(client, async_database_url):
    # A viewer can read the allow-list but cannot add/remove operators (role gate),
    # which would otherwise be a privilege-escalation path (self-promotion to admin).
    asyncio.run(_seed(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/v1/admin/admin-users").status_code == 200
    assert (
        client.post("/v1/admin/admin-users", json={"email": "x@y.com", "role": "admin"}).status_code
        == 403
    )
    assert client.delete("/v1/admin/admin-users/viewer@example.com").status_code == 403


def test_cannot_remove_last_admin(client, admin_session):
    # admin_session seeds the sole admin (admin@example.com); removing it would brick
    # the management plane, so it must 409 rather than delete.
    r = client.delete("/v1/admin/admin-users/admin@example.com")
    assert r.status_code == 409
    # Still present + still admin.
    assert "admin@example.com" in {u["email"] for u in client.get("/v1/admin/admin-users").json()}


def test_cannot_demote_last_admin(client, admin_session):
    r = client.post("/v1/admin/admin-users", json={"email": "admin@example.com", "role": "viewer"})
    assert r.status_code == 409
