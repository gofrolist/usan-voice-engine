"""Foundational auth tests (T004): a compat Bearer key resolves to its organization (or
401 for missing / wrong-scheme / unknown / revoked), and the resolved session is RLS-scoped
to that organization. Exercises the real ``resolve_compat_org`` + ``_scoped_session`` paths
against the usan_app (RLS-subject) role."""

from __future__ import annotations

import secrets
import uuid

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat import auth as compat_auth
from usan_api.compat.errors import CompatError
from usan_api.db.session import _install_default_org_context
from usan_api.repositories.compat_api_keys import hash_token


def _bearer(token: str, scheme: str = "Bearer") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def _app_factory(app_async_database_url: str):
    engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    _install_default_org_context(engine)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _usan_org_id(super_async_url: str) -> uuid.UUID:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(text("SELECT id FROM organizations WHERE slug = 'usan'"))
            ).scalar_one()
    finally:
        await engine.dispose()


async def _seed_key(super_async_url: str, org_id: uuid.UUID, *, status: str = "active") -> str:
    token = "key_" + secrets.token_urlsafe(32)
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO compat_api_keys (organization_id, key_prefix, key_hash, status) "
                    "VALUES (:org, :pfx, :hash, :st)"
                ),
                {"org": org_id, "pfx": token[:8], "hash": hash_token(token), "st": status},
            )
        return token
    finally:
        await engine.dispose()


async def _cleanup_keys(super_async_url: str, org_id: uuid.UUID) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM compat_api_keys WHERE organization_id = :org"), {"org": org_id}
            )
    finally:
        await engine.dispose()


async def test_missing_credentials_rejected(app_async_database_url, app_role_password):
    engine, factory = _app_factory(app_async_database_url)
    try:
        with pytest.raises(CompatError) as exc:
            await compat_auth.resolve_compat_org(factory, None)
        assert exc.value.status_code == 401
    finally:
        await engine.dispose()


async def test_wrong_scheme_rejected(app_async_database_url, app_role_password):
    engine, factory = _app_factory(app_async_database_url)
    try:
        with pytest.raises(CompatError) as exc:
            await compat_auth.resolve_compat_org(factory, _bearer("whatever", scheme="Basic"))
        assert exc.value.status_code == 401
    finally:
        await engine.dispose()


async def test_unknown_token_rejected(app_async_database_url, app_role_password):
    engine, factory = _app_factory(app_async_database_url)
    try:
        with pytest.raises(CompatError) as exc:
            await compat_auth.resolve_compat_org(factory, _bearer("key_nope_not_a_real_token"))
        assert exc.value.status_code == 401
    finally:
        await engine.dispose()


async def test_valid_key_resolves_org(
    async_database_url, app_async_database_url, app_role_password
):
    org_id = await _usan_org_id(async_database_url)
    token = await _seed_key(async_database_url, org_id)
    engine, factory = _app_factory(app_async_database_url)
    try:
        assert await compat_auth.resolve_compat_org(factory, _bearer(token)) == org_id
    finally:
        await _cleanup_keys(async_database_url, org_id)
        await engine.dispose()


async def test_revoked_key_rejected(async_database_url, app_async_database_url, app_role_password):
    org_id = await _usan_org_id(async_database_url)
    token = await _seed_key(async_database_url, org_id, status="revoked")
    engine, factory = _app_factory(app_async_database_url)
    try:
        with pytest.raises(CompatError) as exc:
            await compat_auth.resolve_compat_org(factory, _bearer(token))
        assert exc.value.status_code == 401
    finally:
        await _cleanup_keys(async_database_url, org_id)
        await engine.dispose()


async def test_scoped_session_sets_org_context(
    async_database_url, app_async_database_url, app_role_password
):
    org_id = await _usan_org_id(async_database_url)
    engine, factory = _app_factory(app_async_database_url)
    try:
        async for session in compat_auth._scoped_session(factory, org_id):
            value = (
                await session.execute(text("SELECT current_setting('app.current_org', true)"))
            ).scalar_one()
            assert value == str(org_id)
            break
    finally:
        await engine.dispose()
