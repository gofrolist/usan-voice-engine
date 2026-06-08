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


def test_no_cookie_is_401(client, admin_session):
    client.cookies.clear()
    r = client.get("/v1/admin/profiles")
    assert r.status_code == 401


def test_valid_session_authenticates(client, admin_session):
    r = client.get("/v1/admin/profiles")
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
    r = client.get("/v1/admin/profiles")
    assert r.status_code == 401


def test_role_gate_blocks_viewer(client, async_database_url):
    asyncio.run(_seed(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/v1/admin/profiles").status_code == 200  # viewer can read
    assert client.post("/v1/admin/profiles", json={"name": "x"}).status_code == 403  # not write
