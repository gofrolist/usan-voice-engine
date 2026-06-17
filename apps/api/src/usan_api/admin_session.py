"""Admin session + OAuth-transaction cookies (Google SSO, P3).

Two short-lived HS256 tokens signed with JWT_SIGNING_KEY:

- **session** (`admin_session` cookie): identifies the logged-in operator after a
  successful Google login. SameSite=Strict — the SPA and the post-login redirect are
  same-site, so Strict gives the strongest CSRF posture.
- **tx** (`admin_oauth_tx` cookie): carries the PKCE code_verifier + CSRF state
  between /login and /callback. SameSite=Lax — the callback is reached via Google's
  cross-site top-level redirect, where a Strict cookie would not be sent.

Both are HttpOnly; Secure follows settings.session_cookie_secure.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from starlette.responses import Response

from usan_api.db.base import AdminRole
from usan_api.settings import Settings

SESSION_COOKIE_NAME = "admin_session"
TX_COOKIE_NAME = "admin_oauth_tx"
TX_PATH = "/v1/auth"
TX_TTL_S = 600  # 10 minutes: a login round-trip is seconds; this bounds a stale tab.

INVITE_COOKIE_NAME = "admin_invite_tx"
INVITE_PATH = "/v1/auth"
INVITE_TTL_S = 600  # 10 minutes: rides the OAuth round-trip, then dies.

_ALG = "HS256"


def _key(settings: Settings) -> str:
    return settings.jwt_signing_key.get_secret_value()


def issue_session(
    email: str,
    *,
    active_org_id: uuid.UUID | None,
    role: AdminRole | None,
    is_super_admin: bool,
    acting_as: bool,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": email,
        "active_org": str(active_org_id) if active_org_id else None,
        "role": role.value if role else None,
        "super": is_super_admin,
        "acting_as": acting_as,
        "typ": "admin_session",
        "iat": now,
        "exp": now + timedelta(seconds=settings.admin_session_ttl_s),
    }
    return jwt.encode(payload, _key(settings), algorithm=_ALG)


def decode_session(token: str, settings: Settings) -> dict[str, Any]:
    """Verify the session JWT. Raises jwt.PyJWTError on any problem."""
    claims: dict[str, Any] = jwt.decode(
        token, _key(settings), algorithms=[_ALG], options={"require": ["exp", "sub"]}
    )
    if claims.get("typ") != "admin_session":
        raise jwt.InvalidTokenError("not a session token")
    return claims


def issue_tx(state: str, code_verifier: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "state": state,
        "cv": code_verifier,
        "typ": "oauth_tx",
        "iat": now,
        "exp": now + timedelta(seconds=TX_TTL_S),
    }
    return jwt.encode(payload, _key(settings), algorithm=_ALG)


def decode_tx(token: str, settings: Settings) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token, _key(settings), algorithms=[_ALG], options={"require": ["exp", "state", "cv"]}
    )
    if claims.get("typ") != "oauth_tx":
        raise jwt.InvalidTokenError("not an oauth-tx token")
    return claims


def set_session_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.admin_session_ttl_s,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(resp: Response, settings: Settings) -> None:
    resp.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
    )


def issue_invite(token: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "invite_token": token,
        "typ": "oauth_invite",
        "iat": now,
        "exp": now + timedelta(seconds=INVITE_TTL_S),
    }
    return jwt.encode(payload, _key(settings), algorithm=_ALG)


def decode_invite(token: str, settings: Settings) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token, _key(settings), algorithms=[_ALG], options={"require": ["exp", "invite_token"]}
    )
    if claims.get("typ") != "oauth_invite":
        raise jwt.InvalidTokenError("not an oauth-invite token")
    return claims


def set_invite_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        INVITE_COOKIE_NAME,
        token,
        max_age=INVITE_TTL_S,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=INVITE_PATH,
    )


def clear_invite_cookie(resp: Response, settings: Settings) -> None:
    resp.delete_cookie(
        INVITE_COOKIE_NAME,
        path=INVITE_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


def set_tx_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        TX_COOKIE_NAME,
        token,
        max_age=TX_TTL_S,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=TX_PATH,
    )


def clear_tx_cookie(resp: Response, settings: Settings) -> None:
    resp.delete_cookie(
        TX_COOKIE_NAME,
        path=TX_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
