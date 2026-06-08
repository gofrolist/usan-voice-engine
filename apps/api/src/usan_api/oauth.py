"""Google OAuth 2.0 (Authorization Code + PKCE) + ID-token verification (P3).

The pure parts (PKCE/state generation, authorization-URL building) are unit-tested.
The two network calls are isolated behind functions that router tests monkeypatch:

- exchange_code: POST the auth code to Google's token endpoint, return the id_token.
- verify_id_token: verify the RS256 ID token against Google's JWKS (PyJWKClient),
  check aud/iss/exp + email_verified + optional hosted-domain (hd).

ID-token verification uses PyJWT (pyjwt[crypto]) + Google's JWKS — no google-auth /
requests dependency.
"""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from usan_api.settings import Settings

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - public URL, not a secret
_CERTS_URI = "https://www.googleapis.com/oauth2/v3/certs"
_VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_EXCHANGE_TIMEOUT_S = 10.0

_jwks_client: jwt.PyJWKClient | None = None


class OAuthError(Exception):
    """Any failure exchanging or verifying a Google OAuth credential."""


def new_state() -> str:
    """Opaque, unguessable CSRF state value."""
    return secrets.token_urlsafe(32)


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # ~86 chars, within the 43-128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(settings: Settings, *, state: str, code_challenge: str) -> str:
    params: dict[str, str] = {
        "client_id": settings.google_oauth_client_id or "",
        "redirect_uri": settings.google_oauth_redirect_uri or "",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    if settings.google_oauth_hd:
        params["hd"] = settings.google_oauth_hd
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(settings: Settings, *, code: str, code_verifier: str) -> str:
    """Exchange an auth code for tokens; return the raw id_token. Raises OAuthError."""
    secret = (
        settings.google_oauth_client_secret.get_secret_value()
        if settings.google_oauth_client_secret is not None
        else ""
    )
    data = {
        "code": code,
        "client_id": settings.google_oauth_client_id or "",
        "client_secret": secret,
        "redirect_uri": settings.google_oauth_redirect_uri or "",
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=_EXCHANGE_TIMEOUT_S) as client:
            resp = await client.post(_TOKEN_ENDPOINT, data=data)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthError("token exchange failed") from exc
    id_token = body.get("id_token")
    if not id_token:
        raise OAuthError("token response had no id_token")
    return str(id_token)


def _certs() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # PyJWKClient caches keys and refreshes on unknown kid; one instance is fine.
        _jwks_client = jwt.PyJWKClient(_CERTS_URI)
    return _jwks_client


def verify_id_token(settings: Settings, raw_token: str) -> dict[str, Any]:
    """Verify a Google ID token and return its claims. Raises OAuthError on any failure."""
    try:
        signing_key = _certs().get_signing_key_from_jwt(raw_token)
        claims: dict[str, Any] = jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.google_oauth_client_id,
            options={"require": ["exp", "aud", "iss", "email"]},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        raise OAuthError("id token verification failed") from exc
    if claims.get("iss") not in _VALID_ISSUERS:
        raise OAuthError("unexpected token issuer")
    if not claims.get("email_verified"):
        raise OAuthError("email not verified")
    if settings.google_oauth_hd and claims.get("hd") != settings.google_oauth_hd:
        raise OAuthError("hosted-domain mismatch")
    return claims
