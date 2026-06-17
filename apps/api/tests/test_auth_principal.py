"""Task B2: AdminPrincipal resolution, membership re-validation, act-as guard,
get_tenant_db (409 + RLS context), and require_super_admin.

These tests mount a tiny app whose routes depend on each dependency under test,
mint a session cookie via issue_session, and assert the resolved principal /
status codes. require_admin_session reads the GLOBAL admin_users + memberships
tables, so its db dependency is overridden with a superuser session here. The
admin_users / memberships rows are seeded via the superuser engine; get_tenant_db
opens its OWN session through get_session_factory() (the usan_app RLS-subject role,
since the env DATABASE_URL points at app_database_url).
"""

import asyncio
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import (
    AdminPrincipal,
    get_tenant_db,
    require_admin_session,
    require_super_admin,
)
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


@pytest.fixture
def principal_app(
    database_url: str,
    async_database_url: str,
    app_database_url: str,
    monkeypatch,
) -> TestClient:
    """A minimal app exposing one route per dependency under test.

    Mirrors the `client` fixture's env so get_session_factory() (used inside
    get_tenant_db) connects as the usan_app RLS-subject role. The superuser
    `get_db` override backs require_admin_session's reads of the global tables.
    """
    monkeypatch.setenv("DATABASE_URL", app_database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    super_engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(super_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.dependency_overrides[get_db] = _override_get_db

    @app.get("/principal")
    async def _principal(
        principal: AdminPrincipal = Depends(require_admin_session),
    ) -> dict:
        return {
            "email": principal.email,
            "active_org_id": (str(principal.active_org_id) if principal.active_org_id else None),
            "role": principal.role.value if principal.role else None,
            "is_super_admin": principal.is_super_admin,
            "acting_as": principal.acting_as,
        }

    @app.get("/tenant-db")
    async def _tenant_db(db=Depends(get_tenant_db)) -> dict:
        org = (
            await db.execute(text("SELECT current_setting('app.current_org', true)"))
        ).scalar_one()
        return {"current_org": org}

    @app.get("/super")
    async def _super(
        principal: AdminPrincipal = Depends(require_super_admin),
    ) -> dict:
        return {"email": principal.email}

    try:
        yield TestClient(app)
    finally:
        asyncio.run(super_engine.dispose())
        from usan_api.db.session import dispose_engine

        asyncio.run(dispose_engine())
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        from usan_api.tenant_context import _clear_default_org_cache

        _clear_default_org_cache()


def _seed_user(async_database_url: str, email: str, *, is_super: bool = False) -> uuid.UUID:
    """Seed an identity + return the seeded usan (default) org id."""

    async def _run() -> uuid.UUID:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                org_id = (
                    await conn.execute(text("SELECT id FROM organizations WHERE slug='usan'"))
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                        "VALUES (:e, :s, 'active', 'test') "
                        "ON CONFLICT (email) DO UPDATE SET is_super_admin = EXCLUDED.is_super_admin"
                    ),
                    {"e": email.lower(), "s": is_super},
                )
            return org_id
        finally:
            await engine.dispose()

    return asyncio.run(_run())


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


def _delete_membership(async_database_url: str, email: str, org_id: uuid.UUID) -> None:
    async def _run() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM memberships WHERE email=:e AND organization_id=:o"),
                    {"e": email.lower(), "o": org_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _cookie(email: str, **kw) -> dict[str, str]:
    token = issue_session(email, settings=get_settings(), **kw)
    return {SESSION_COOKIE_NAME: token}


def test_member_principal_resolves_role_from_membership(principal_app, async_database_url):
    org = _seed_user(async_database_url, "member@x.com")
    _add_membership(async_database_url, "member@x.com", org, "viewer")
    # Session claims a stale role=admin; the principal must trust the live membership.
    cookies = _cookie(
        "member@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
    )
    r = principal_app.get("/principal", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "viewer"
    assert body["acting_as"] is False
    assert body["active_org_id"] == str(org)


def test_deleted_membership_is_revoked(principal_app, async_database_url):
    org = _seed_user(async_database_url, "gone@x.com")
    _add_membership(async_database_url, "gone@x.com", org, "admin")
    cookies = _cookie(
        "gone@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
    )
    assert principal_app.get("/principal", cookies=cookies).status_code == 200
    _delete_membership(async_database_url, "gone@x.com", org)
    assert principal_app.get("/principal", cookies=cookies).status_code == 403


def test_act_as_requires_super_admin(principal_app, async_database_url):
    org = _seed_user(async_database_url, "fake@x.com", is_super=False)
    # acting_as=True but not super -> impersonation forgery, rejected.
    cookies = _cookie(
        "fake@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=True,
    )
    assert principal_app.get("/principal", cookies=cookies).status_code == 401


def test_act_as_super_admin_resolves_admin_role(principal_app, async_database_url):
    org = _seed_user(async_database_url, "boss@x.com", is_super=True)
    cookies = _cookie(
        "boss@x.com",
        active_org_id=org,
        role=None,
        is_super_admin=True,
        acting_as=True,
    )
    r = principal_app.get("/principal", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["acting_as"] is True
    assert body["is_super_admin"] is True


def test_get_tenant_db_409_without_active_org(principal_app, async_database_url):
    _seed_user(async_database_url, "noorg@x.com", is_super=True)
    cookies = _cookie(
        "noorg@x.com",
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
    )
    r = principal_app.get("/tenant-db", cookies=cookies)
    assert r.status_code == 409


def test_get_tenant_db_sets_org_context(principal_app, async_database_url):
    org = _seed_user(async_database_url, "ctx@x.com")
    _add_membership(async_database_url, "ctx@x.com", org, "admin")
    cookies = _cookie(
        "ctx@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
    )
    r = principal_app.get("/tenant-db", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["current_org"] == str(org)


def test_require_super_admin_blocks_non_super(principal_app, async_database_url):
    org = _seed_user(async_database_url, "plain@x.com", is_super=False)
    _add_membership(async_database_url, "plain@x.com", org, "admin")
    cookies = _cookie(
        "plain@x.com",
        active_org_id=org,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
    )
    assert principal_app.get("/super", cookies=cookies).status_code == 403


def test_require_super_admin_allows_super(principal_app, async_database_url):
    _seed_user(async_database_url, "admin2@x.com", is_super=True)
    cookies = _cookie(
        "admin2@x.com",
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
    )
    r = principal_app.get("/super", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["email"] == "admin2@x.com"
