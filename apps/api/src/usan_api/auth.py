import hmac
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_session import SESSION_COOKIE_NAME, decode_session
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.settings import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

# RFC 7235 §3.1: a 401 MUST carry a WWW-Authenticate challenge so standards-compliant
# clients and gateways know how to authenticate.
_WWW_AUTH = {"WWW-Authenticate": "Bearer"}

# Cookie-borne token types (admin session, OAuth tx) are signed with the same
# JWT_SIGNING_KEY as the agent service/worker bearer tokens. The agent-plane verifiers
# must reject them so an operator's session cookie cannot be lifted from the browser
# and replayed as a Bearer worker/service token (cross-token-type confusion).
_COOKIE_TOKEN_TYPES = frozenset({"admin_session", "oauth_tx"})


def _reject_cookie_token(claims: dict[str, Any]) -> None:
    if claims.get("typ") in _COOKIE_TOKEN_TYPES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers=_WWW_AUTH,
        )


def require_operator_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate an operator on the management plane via a static bearer token.

    Guards human/back-office routes (contacts, DNC, outbound enqueue/lookup). The
    presented token is compared to OPERATOR_API_KEY in constant time. The mismatch
    message is deliberately generic so it leaks nothing about why it failed.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    if not hmac.compare_digest(
        credentials.credentials, settings.operator_api_key.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator token",
            headers=_WWW_AUTH,
        )


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a service-to-service JWT (HS256). Returns the decoded claims.

    Used for agent→API calls. The token must be signed with JWT_SIGNING_KEY and
    carry `exp` and `call_id` claims. The caller is responsible for checking that
    the `call_id` claim matches the resource being mutated.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp", "call_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers=_WWW_AUTH,
        ) from exc
    _reject_cookie_token(claims)
    return claims


def require_worker_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a worker JWT that is NOT yet scoped to a specific call.

    Inbound calls have no call_id until the API mints one, so the agent cannot
    present a call-scoped token to the inbound-create endpoint. This verifies the
    HS256 signature + exp only; it proves the caller holds JWT_SIGNING_KEY (our
    agent worker). Endpoints using it CREATE a resource rather than mutate a named
    one; for mutating an existing call, use require_service_token.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers=_WWW_AUTH,
        ) from exc
    _reject_cookie_token(claims)
    return claims


_COOKIE_AUTH = {"WWW-Authenticate": "Cookie"}


@dataclass(frozen=True)
class AdminPrincipal:
    """The authenticated admin operator for the current request (P2, org-aware).

    ``role`` is the caller's role in ``active_org_id`` (None when no org is active).
    ``is_super_admin`` and ``acting_as`` ride the session claims; act-as means the
    active org came from a super-admin switch, not a membership.
    """

    email: str
    active_org_id: uuid.UUID | None
    role: AdminRole | None
    is_super_admin: bool
    acting_as: bool


async def require_admin_session(
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    """Authenticate an admin operator from the session cookie.

    The cookie proves authentication (Google verified the human) and carries the
    active org + role + super/act-as flags (P2 / migration 0033 moved role off the
    admin_users row onto memberships). The global ``admin_users`` row still provides
    live authorization: a removed/blocked operator is rejected immediately even while
    their cookie is unexpired. 401 (not 403) on a missing/invalid/expired cookie or a
    no-longer-allow-listed/inactive email. Membership re-validation against the active
    org is layered on in Task B2.
    """
    if not session_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing session",
            headers=_COOKIE_AUTH,
        )
    try:
        claims = decode_session(session_cookie, settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session",
            headers=_COOKIE_AUTH,
        ) from exc
    email = str(claims["sub"]).lower()
    user = await admin_users_repo.get_admin_user(db, email)
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authorized",
            headers=_COOKIE_AUTH,
        )
    active_org_raw = claims.get("active_org")
    active_org_id = uuid.UUID(str(active_org_raw)) if active_org_raw else None
    role_raw = claims.get("role")
    role = AdminRole(role_raw) if role_raw else None
    return AdminPrincipal(
        email=email,
        active_org_id=active_org_id,
        role=role,
        is_super_admin=bool(claims.get("super")),
        acting_as=bool(claims.get("acting_as")),
    )


def require_admin_role(
    required: AdminRole,
) -> Callable[..., Coroutine[Any, Any, AdminPrincipal]]:
    """Dependency factory: require at least `required` role (admin > viewer)."""

    async def _dep(principal: AdminPrincipal = Depends(require_admin_session)) -> AdminPrincipal:
        if required is AdminRole.ADMIN and principal.role is not AdminRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
        return principal

    return _dep
