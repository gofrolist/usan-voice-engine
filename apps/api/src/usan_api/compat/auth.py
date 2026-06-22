"""Compat bearer-key auth -> org-scoped RLS session (feature 003).

Mirrors ``auth.get_tenant_db`` but resolves the organization from a ``compat_api_keys``
Bearer token (prefix lookup + constant-time hash compare) instead of an admin session. The
dependency runs on a SINGLE session/connection per request: match the key on the non-RLS
table under the connect-baseline context, then scope that SAME session to the key's org via
the ``after_begin`` re-apply seam. One connection per request keeps it correct under the
TestClient's per-request event loop. Missing / invalid / revoked key -> ``CompatError(401)``.

``resolve_compat_org`` / ``_scoped_session`` are split-out helpers the test suite exercises
directly (with a per-test, event-loop-local factory) so the 401 + RLS paths stay real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api.compat.errors import CompatError
from usan_api.db.models import CompatApiKey
from usan_api.db.session import get_session_factory
from usan_api.repositories import compat_api_keys as keys_repo
from usan_api.tenant_context import set_tenant_context

_compat_bearer = HTTPBearer(auto_error=False)
_PREFIX_LEN = 8
_UNAUTHORIZED = "missing or invalid api key"


def _require_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise CompatError(401, _UNAUTHORIZED)
    return credentials.credentials


async def _match_key(session: AsyncSession, token: str) -> CompatApiKey:
    """Constant-time match the token against active candidates sharing its prefix, so a bad /
    revoked / absent key is indistinguishable (no token-probing oracle)."""
    import hmac

    candidates = await keys_repo.lookup_active_by_prefix(session, token[:_PREFIX_LEN])
    token_hash = keys_repo.hash_token(token)
    matched = next((c for c in candidates if hmac.compare_digest(c.key_hash, token_hash)), None)
    if matched is None:
        raise CompatError(401, _UNAUTHORIZED)
    return matched


def _install_reapply(session: AsyncSession, org: str) -> Any:
    """Register (and return) the after_begin listener that re-applies the org context on
    every new transaction — set_tenant_context is is_local=true (reverts at COMMIT)."""

    def _reapply(_session: Any, _transaction: Any, connection: Any) -> None:
        connection.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})

    event.listen(session.sync_session, "after_begin", _reapply)
    return _reapply


async def resolve_compat_org(
    factory: async_sessionmaker[AsyncSession],
    credentials: HTTPAuthorizationCredentials | None,
) -> uuid.UUID:
    """Authenticate the Bearer token -> the key's organization_id, or raise CompatError(401).
    Opens its own short-lived session (exercised directly by the auth tests)."""
    token = _require_bearer(credentials)
    async with factory() as session:
        matched = await _match_key(session, token)
        org_id = matched.organization_id
        key_id = matched.id
        await keys_repo.touch_last_used(session, key_id)
        await session.commit()
    logger.bind(org_id=str(org_id), compat_key_id=str(key_id)).info(
        "compat request authenticated org={org_id} key={compat_key_id}"
    )
    return org_id


async def _scoped_session(
    factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """An RLS-scoped session for ``org_id`` (exercised directly by the RLS-isolation tests)."""
    async with factory() as session:
        reapply = _install_reapply(session, str(org_id))
        try:
            await set_tenant_context(session, org_id)
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            event.remove(session.sync_session, "after_begin", reapply)


async def get_compat_db(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_compat_bearer),
) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: Bearer compat key -> org-scoped RLS session, on ONE connection.

    A SINGLE ``async with`` generator (mirrors ``auth.get_tenant_db``) so the session is
    always closed within the request's own event loop — a nested ``async for`` delegation
    does not reliably close it on GeneratorExit, leaving a connection to be torn down later
    on a dead loop (TestClient uses a fresh loop per request). 401 on a bad key; stashes the
    org id on ``request.state`` for the per-op audit (FR-055).
    """
    token = _require_bearer(credentials)
    async with get_session_factory()() as session:
        matched = await _match_key(session, token)
        org_id = matched.organization_id
        request.state.compat_org_id = str(org_id)
        reapply = _install_reapply(session, str(org_id))
        try:
            await set_tenant_context(session, org_id)
            matched.last_used_at = datetime.now(UTC)  # best-effort; persists on a write commit
            logger.bind(compat_org_id=str(org_id), compat_key_id=str(matched.id)).info(
                "compat request authenticated org={compat_org_id} key={compat_key_id}"
            )
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            event.remove(session.sync_session, "after_begin", reapply)
