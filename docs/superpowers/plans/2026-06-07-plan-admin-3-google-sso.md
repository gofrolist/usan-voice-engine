# Admin UI P3 — Google SSO + Audit Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the temporary `OPERATOR_API_KEY` guard on the admin plane with named-user Google SSO — verified ID tokens, an app-issued session cookie, a DB allow-list with bootstrap seeding, and real audit attribution.

**Architecture:** App-level SSO verified in `apps/api` (not IAP). `GET /v1/auth/login` starts an OAuth 2.0 Authorization-Code-+-PKCE flow against Google; `GET /v1/auth/callback` exchanges the code, verifies the Google ID token, checks the email against the `admin_users` allow-list, and issues the API's *own* short-lived HS256 session JWT as an HttpOnly/Secure/SameSite=Strict cookie. `/v1/admin/*` swaps from the static operator token to a `require_admin_session` dependency that re-checks the allow-list on every request (immediate revocation + live role). The audit log records the authenticated operator's email.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (async), PyJWT (`pyjwt[crypto]` — HS256 for our session, RS256 + `PyJWKClient` for Google ID tokens), httpx (token exchange), pytest + Postgres testcontainer.

**Scope:** `api` only. No migration (the `admin_users` table + `AdminUser` model already shipped in Alembic `0010` / P1). No agent changes (the `apps/api ⊥ services/agent` boundary is untouched). Infra wiring (compose, Caddy, Terraform, Secret Manager seeding, the GCP OAuth redirect URI) is **P5** — see design §11. All new SSO env is **optional**: the API boots without it and the auth endpoints return `503` until configured.

**Branch:** `admin-ui-p3`, stacked on `admin-ui-p2` (P1/P2 are open, unmerged). PR base = `admin-ui-p2`.

---

## Design decisions (read before starting)

1. **Google ID-token verification uses PyJWT, not google-auth.** `jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")` fetches Google's JWKS (via urllib, no `requests`); `jwt.decode(..., algorithms=["RS256"])` needs `cryptography`, pulled in by the `pyjwt[crypto]` extra. This reuses libraries already in the lock and adds no heavy transitive deps.

2. **`require_admin_session` re-checks the DB allow-list every request.** The session cookie proves *authentication* (Google verified the human); the `admin_users` row provides current *authorization* (still allow-listed) and the *live* role (a removal or role change takes effect immediately, not at token expiry). One indexed PK lookup per admin request is negligible on this low-traffic plane. The role in the cookie is informational only — the DB role wins.

3. **Clean swap, not dual-auth.** `/v1/admin/profiles/*` drops `require_operator_token` entirely (design §14: "swap admin routes from `OPERATOR_API_KEY` to session auth"). `OPERATOR_API_KEY` remains the machine-plane guard for elders/DNC/calls. After P3, the admin API requires an SSO session (or, for scripted use, a manually-minted session JWT). This is acceptable because the admin UI is pre-production; SSO secrets land in P5 before it goes live.

4. **Two cookies, two `SameSite` values.**
   - **Session cookie** (`admin_session`): `SameSite=Strict`, `Path=/`, lifetime `ADMIN_SESSION_TTL_S`. The post-login redirect and all SPA calls are same-site, so Strict works and maximizes CSRF protection.
   - **OAuth transaction cookie** (`admin_oauth_tx`): carries the PKCE `code_verifier` + CSRF `state` between `/login` and `/callback`. **Must be `SameSite=Lax`** — the callback arrives via Google's cross-site top-level redirect, and a Strict cookie would not be sent, breaking the state check. `Path=/v1/auth`, 10-minute lifetime.
   - Both `HttpOnly`; `Secure` is driven by `SESSION_COOKIE_SECURE` (default `True`; the test client and local-http set `False`, because the stdlib cookie jar will not return a `Secure` cookie over `http://`).

5. **Audit actions:** `auth.login` (success), `auth.denied` (verified Google identity not on the allow-list), `auth.logout`, plus `admin_user.add` / `admin_user.remove`. Operator emails are not patient PHI, so recording them is correct and required for attribution.

---

## File structure

**New files:**

- `apps/api/src/usan_api/oauth.py` — Google OAuth helpers: PKCE/state generation, authorization-URL builder, async code exchange, ID-token verification. Pure/deterministic parts unit-tested; network calls (`exchange_code`, `verify_id_token`) monkeypatched in router tests.
- `apps/api/src/usan_api/admin_session.py` — mint/verify the session JWT and the OAuth-tx JWT; cookie set/clear helpers. One responsibility: cookie + token plumbing.
- `apps/api/src/usan_api/repositories/admin_users.py` — allow-list data access: get / list / add / remove / `seed_bootstrap`.
- `apps/api/src/usan_api/schemas/auth.py` — `MeResponse`, `AdminUserOut`, `AdminUserCreate`.
- `apps/api/src/usan_api/routers/auth.py` — `/v1/auth/login`, `/callback`, `/logout`, `/me`.
- `apps/api/src/usan_api/routers/admin_users.py` — `/v1/admin/admin-users` GET/POST/DELETE (manage the allow-list).
- `apps/api/tests/test_admin_session_unit.py` — unit tests for `admin_session.py` + `oauth.py` pure parts.
- `apps/api/tests/test_admin_users_repo.py` — repo + bootstrap-seed tests.
- `apps/api/tests/test_require_admin_session.py` — the session/role dependencies (cookie present/absent/expired/revoked; role gate).
- `apps/api/tests/test_auth_flow.py` — login redirect, callback success/denied, logout, me (oauth functions monkeypatched).
- `apps/api/tests/test_admin_users_api.py` — admin-users CRUD endpoints.
- `apps/api/tests/test_bootstrap_seed.py` — lifespan seeding from `ADMIN_BOOTSTRAP_EMAILS`.

**Modified files:**

- `apps/api/pyproject.toml` — `pyjwt>=2.10.0` → `pyjwt[crypto]>=2.10.0`.
- `apps/api/src/usan_api/settings.py` — new optional SSO env + `sso_enabled` / `bootstrap_emails_list` helpers + extend `_blank_to_none`.
- `apps/api/src/usan_api/auth.py` — add `AdminPrincipal`, `require_admin_session`, `require_admin_role`.
- `apps/api/src/usan_api/admin_actor.py` — `get_actor_email` now returns the session email.
- `apps/api/src/usan_api/routers/admin_profiles.py` — router guard `require_operator_token` → `require_admin_session`; mutations add `require_admin_role(AdminRole.ADMIN)`.
- `apps/api/src/usan_api/main.py` — include the two new routers; seed the allow-list in `lifespan`.
- `apps/api/src/usan_api/ratelimit.py` — throttle `/v1/auth/*` (pre-auth, like the rest of the operator plane).
- `apps/api/tests/conftest.py` — `SESSION_COOKIE_SECURE=false` in the client env; add an `sso_client` fixture; add an `admin_session` cookie fixture (seeds an allow-listed admin).
- `apps/api/tests/test_admin_profiles_api.py` — migrate from `_OP` header to the `admin_session` cookie; repurpose the auth-required test.
- `apps/api/tests/test_app_security.py` — comment accuracy only (assertions already hold).

---

## Task 1: Add the `crypto` extra to PyJWT

**Files:**
- Modify: `apps/api/pyproject.toml`

- [ ] **Step 1: Edit the dependency**

In `apps/api/pyproject.toml`, change the PyJWT line:

```toml
    "pyjwt[crypto]>=2.10.0",
```

(was `"pyjwt>=2.10.0"`). This guarantees `cryptography` is a declared dependency — required for RS256 verification of Google ID tokens.

- [ ] **Step 2: Sync and verify**

```bash
cd apps/api && uv sync
uv run python -c "import jwt; from jwt import PyJWKClient; import cryptography; print('crypto ok')"
```

Expected: `crypto ok` (no ImportError).

- [ ] **Step 3: Commit**

```bash
git add apps/api/pyproject.toml apps/api/uv.lock
git commit -m "chore(api): add pyjwt[crypto] extra for RS256 ID-token verification"
```

---

## Task 2: SSO settings

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_settings.py`:

```python
def test_sso_disabled_by_default(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.sso_enabled is False
        assert s.bootstrap_emails_list == []
        assert s.session_cookie_secure is True
    finally:
        get_settings.cache_clear()


def test_sso_enabled_and_bootstrap_parsing(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
        "GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
        "GOOGLE_OAUTH_REDIRECT_URI": "https://admin.example.com/v1/auth/callback",
        "ADMIN_BOOTSTRAP_EMAILS": " Alice@Example.com , bob@example.com ,",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.sso_enabled is True
        # normalized: trimmed, lowercased, blanks dropped
        assert s.bootstrap_emails_list == ["alice@example.com", "bob@example.com"]
    finally:
        get_settings.cache_clear()
```

Confirm `test_settings.py` already imports `get_settings` (it does — it tests env parsing). If not, add `from usan_api.settings import get_settings`.

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k "sso" -v`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'sso_enabled'`).

- [ ] **Step 3: Implement**

In `apps/api/src/usan_api/settings.py`, add these fields to `Settings` (place them after `operator_api_key`):

```python
    # --- Admin UI / Google SSO (P3). All optional: SSO is off unless the OAuth
    # client id, secret, and redirect URI are all set (sso_enabled). Infra wiring
    # (Secret Manager + compose) is P5; the API boots fine without these.
    google_oauth_client_id: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="GOOGLE_OAUTH_CLIENT_SECRET"
    )
    google_oauth_redirect_uri: str | None = Field(default=None, alias="GOOGLE_OAUTH_REDIRECT_URI")
    # Optional G Suite hosted-domain restriction (the `hd` claim). When set, an ID
    # token whose hd != this value is rejected even if the email is allow-listed.
    google_oauth_hd: str | None = Field(default=None, alias="GOOGLE_OAUTH_HD")
    # Comma-separated emails seeded into admin_users (role=admin) on first boot.
    admin_bootstrap_emails: str = Field(default="", alias="ADMIN_BOOTSTRAP_EMAILS")
    # Session-cookie lifetime. Removal/role changes still take effect immediately
    # (require_admin_session re-checks the DB), so this only bounds re-login.
    admin_session_ttl_s: int = Field(
        default=28800, ge=300, le=86400, alias="ADMIN_SESSION_TTL_S"
    )
    # Set the Secure flag on the session/tx cookies. Default True for prod (Caddy
    # terminates TLS). The test client + local http serve over http, where a Secure
    # cookie is never returned by the client, so they set this false.
    session_cookie_secure: bool = Field(default=True, alias="SESSION_COOKIE_SECURE")
    # Where /v1/auth/callback redirects the browser after a successful login (the SPA).
    admin_post_login_redirect: str = Field(default="/", alias="ADMIN_POST_LOGIN_REDIRECT")
```

Add the four new optional string fields to the existing `_blank_to_none` validator's field list so compose's `${VAR:-}` empties become `None`:

```python
    @field_validator(
        "telnyx_caller_id",
        "telnyx_sip_username",
        "telnyx_sip_password",
        "livekit_sip_outbound_trunk_id",
        "google_oauth_client_id",
        "google_oauth_redirect_uri",
        "google_oauth_hd",
        "phi_retention_days",
        mode="before",
    )
```

(Note: `google_oauth_client_secret` is `SecretStr | None`; Pydantic coerces `""` to a `SecretStr("")` which `sso_enabled` treats as falsy via `.get_secret_value()`, so it does not need the blank-to-none validator.)

Add two properties to `Settings` (after `livekit_http_url`):

```python
    @property
    def sso_enabled(self) -> bool:
        """True when Google SSO is fully configured (client id + secret + redirect)."""
        secret = (
            self.google_oauth_client_secret.get_secret_value()
            if self.google_oauth_client_secret is not None
            else ""
        )
        return bool(self.google_oauth_client_id and secret and self.google_oauth_redirect_uri)

    @property
    def bootstrap_emails_list(self) -> list[str]:
        """Allow-list bootstrap emails, trimmed + lowercased, blanks dropped."""
        return [e.strip().lower() for e in self.admin_bootstrap_emails.split(",") if e.strip()]
```

- [ ] **Step 4: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k "sso" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings.py
git commit -m "feat(api): SSO settings (oauth client, bootstrap emails, session cookie knobs)"
```

---

## Task 3: `admin_session.py` — session + tx tokens and cookies

**Files:**
- Create: `apps/api/src/usan_api/admin_session.py`
- Test: `apps/api/tests/test_admin_session_unit.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_admin_session_unit.py`:

```python
import datetime

import jwt
import pytest

from usan_api.admin_session import (
    SESSION_COOKIE_NAME,
    decode_session,
    decode_tx,
    issue_session,
    issue_tx,
)
from usan_api.db.base import AdminRole
from usan_api.settings import Settings

_ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_ENV, **overrides})  # type: ignore[arg-type]


def test_session_round_trip():
    s = _settings()
    token = issue_session("admin@example.com", AdminRole.ADMIN, s)
    claims = decode_session(token, s)
    assert claims["sub"] == "admin@example.com"
    assert claims["role"] == "admin"
    assert claims["typ"] == "admin_session"


def test_session_rejects_wrong_key():
    token = issue_session("admin@example.com", AdminRole.ADMIN, _settings())
    other = _settings(JWT_SIGNING_KEY="x" * 32)
    with pytest.raises(jwt.PyJWTError):
        decode_session(token, other)


def test_session_rejects_wrong_typ():
    s = _settings()
    # A tx token must not be accepted as a session.
    tx = issue_tx("state123", "verifier123", s)
    with pytest.raises(jwt.PyJWTError):
        decode_session(tx, s)


def test_expired_session_rejected():
    s = _settings()
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    token = jwt.encode(
        {"sub": "a@e.com", "role": "admin", "typ": "admin_session", "exp": past},
        "s" * 32,
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_session(token, s)


def test_tx_round_trip():
    s = _settings()
    tx = issue_tx("the-state", "the-verifier", s)
    data = decode_tx(tx, s)
    assert data["state"] == "the-state"
    assert data["cv"] == "the-verifier"


def test_cookie_name_constant():
    assert SESSION_COOKIE_NAME == "admin_session"
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_admin_session_unit.py -v`
Expected: FAIL (`ModuleNotFoundError: usan_api.admin_session`).

- [ ] **Step 3: Implement**

Create `apps/api/src/usan_api/admin_session.py`:

```python
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

_ALG = "HS256"


def _key(settings: Settings) -> str:
    return settings.jwt_signing_key.get_secret_value()


def issue_session(email: str, role: AdminRole, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": email,
        "role": role.value,
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
```

- [ ] **Step 4: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_admin_session_unit.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/admin_session.py apps/api/tests/test_admin_session_unit.py
git commit -m "feat(api): admin session + oauth-tx cookie tokens (HS256, SameSite-aware)"
```

---

## Task 4: `oauth.py` — Google OAuth + PKCE + ID-token verification

**Files:**
- Create: `apps/api/src/usan_api/oauth.py`
- Test: `apps/api/tests/test_admin_session_unit.py` (append pure-part tests)

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_admin_session_unit.py`:

```python
from urllib.parse import parse_qs, urlsplit

from usan_api import oauth


def test_pkce_pair_is_valid():
    verifier, challenge = oauth.new_pkce()
    # RFC 7636: verifier 43-128 chars; challenge is url-safe base64 of SHA256(verifier).
    assert 43 <= len(verifier) <= 128
    import base64
    import hashlib

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()


def test_state_is_random():
    assert oauth.new_state() != oauth.new_state()


def test_authorization_url_has_required_params():
    s = _settings(
        GOOGLE_OAUTH_CLIENT_ID="cid.apps.googleusercontent.com",
        GOOGLE_OAUTH_CLIENT_SECRET="secret",
        GOOGLE_OAUTH_REDIRECT_URI="https://admin.example.com/v1/auth/callback",
    )
    url = oauth.build_authorization_url(s, state="STATE", code_challenge="CHALLENGE")
    parts = urlsplit(url)
    assert parts.netloc == "accounts.google.com"
    q = parse_qs(parts.query)
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["https://admin.example.com/v1/auth/callback"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["openid email profile"]
    assert q["state"] == ["STATE"]
    assert q["code_challenge"] == ["CHALLENGE"]
    assert q["code_challenge_method"] == ["S256"]
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_admin_session_unit.py -k "pkce or state or authorization" -v`
Expected: FAIL (`ModuleNotFoundError: usan_api.oauth`).

- [ ] **Step 3: Implement**

Create `apps/api/src/usan_api/oauth.py`:

```python
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
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
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
```

- [ ] **Step 4: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_admin_session_unit.py -v`
Expected: PASS (all, including the new pure-OAuth tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/oauth.py apps/api/tests/test_admin_session_unit.py
git commit -m "feat(api): Google OAuth PKCE + ID-token verification (PyJWKClient, no google-auth)"
```

---

## Task 5: `admin_users` repository + bootstrap seeding

**Files:**
- Create: `apps/api/src/usan_api/repositories/admin_users.py`
- Test: `apps/api/tests/test_admin_users_repo.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_admin_users_repo.py`:

```python
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import AdminRole
from usan_api.repositories import admin_users as repo


def _factory(async_database_url: str):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _truncate(async_database_url: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE admin_users RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


def test_add_get_remove_round_trip(async_database_url):
    async def _run():
        await _truncate(async_database_url)
        engine, factory = _factory(async_database_url)
        try:
            async with factory() as db:
                await repo.add_admin_user(
                    db, email="Alice@Example.com", role=AdminRole.ADMIN, added_by="me@x.com"
                )
                await db.commit()
            async with factory() as db:
                # email is normalized to lowercase on write.
                u = await repo.get_admin_user(db, "alice@example.com")
                assert u is not None
                assert u.role is AdminRole.ADMIN
                assert u.added_by == "me@x.com"
                users = await repo.list_admin_users(db)
                assert [x.email for x in users] == ["alice@example.com"]
            async with factory() as db:
                removed = await repo.remove_admin_user(db, "alice@example.com")
                await db.commit()
                assert removed is True
            async with factory() as db:
                assert await repo.get_admin_user(db, "alice@example.com") is None
                assert await repo.remove_admin_user(db, "alice@example.com") is False
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_seed_bootstrap_is_idempotent(async_database_url):
    async def _run():
        await _truncate(async_database_url)
        engine, factory = _factory(async_database_url)
        try:
            async with factory() as db:
                n1 = await repo.seed_bootstrap(db, ["a@x.com", "b@x.com"])
                await db.commit()
                assert n1 == 2
            async with factory() as db:
                # Re-seeding inserts nothing new and never errors on existing rows.
                n2 = await repo.seed_bootstrap(db, ["a@x.com", "b@x.com", "c@x.com"])
                await db.commit()
                assert n2 == 1
            async with factory() as db:
                emails = {u.email for u in await repo.list_admin_users(db)}
                assert emails == {"a@x.com", "b@x.com", "c@x.com"}
        finally:
            await engine.dispose()

    asyncio.run(_run())
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_admin_users_repo.py -v`
Expected: FAIL (`ModuleNotFoundError: usan_api.repositories.admin_users`).

- [ ] **Step 3: Implement**

Create `apps/api/src/usan_api/repositories/admin_users.py`:

```python
"""Allow-list data access for admin SSO (P3).

The admin_users table is the source of truth for who may log in and at what role.
require_admin_session re-checks it on every admin request, so a removal here revokes
access immediately. Emails are stored lowercase (the PK) for case-insensitive match.
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser


def _norm(email: str) -> str:
    return email.strip().lower()


async def get_admin_user(db: AsyncSession, email: str) -> AdminUser | None:
    return await db.get(AdminUser, _norm(email))


async def list_admin_users(db: AsyncSession) -> list[AdminUser]:
    result = await db.execute(select(AdminUser).order_by(AdminUser.email))
    return list(result.scalars().all())


async def add_admin_user(
    db: AsyncSession, *, email: str, role: AdminRole, added_by: str | None
) -> AdminUser:
    """Insert (or update the role of) an allow-listed operator. Caller commits."""
    norm = _norm(email)
    stmt = (
        pg_insert(AdminUser)
        .values(email=norm, role=role, added_by=added_by)
        .on_conflict_do_update(index_elements=["email"], set_={"role": role})
    )
    await db.execute(stmt)
    await db.flush()
    user = await db.get(AdminUser, norm)
    assert user is not None  # just inserted/updated
    return user


async def remove_admin_user(db: AsyncSession, email: str) -> bool:
    """Delete an operator. Returns False if the email was not present. Caller commits."""
    result = await db.execute(delete(AdminUser).where(AdminUser.email == _norm(email)))
    await db.flush()
    return (result.rowcount or 0) > 0


async def seed_bootstrap(db: AsyncSession, emails: list[str]) -> int:
    """Insert any missing bootstrap emails as admins. Returns the count inserted.

    Idempotent: ON CONFLICT DO NOTHING leaves existing rows (and their possibly
    edited roles) untouched. Caller commits.
    """
    inserted = 0
    for email in emails:
        norm = _norm(email)
        if not norm:
            continue
        stmt = (
            pg_insert(AdminUser)
            .values(email=norm, role=AdminRole.ADMIN, added_by="bootstrap")
            .on_conflict_do_nothing(index_elements=["email"])
        )
        result = await db.execute(stmt)
        inserted += result.rowcount or 0
    await db.flush()
    return inserted
```

- [ ] **Step 4: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_admin_users_repo.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/repositories/admin_users.py apps/api/tests/test_admin_users_repo.py
git commit -m "feat(api): admin_users repository + idempotent bootstrap seeding"
```

---

## Task 6: Auth dependencies + actor swap + conftest fixtures

**Files:**
- Modify: `apps/api/src/usan_api/auth.py`
- Modify: `apps/api/src/usan_api/admin_actor.py`
- Modify: `apps/api/tests/conftest.py`
- Test: `apps/api/tests/test_require_admin_session.py`

> **Sequencing:** `test_require_admin_session.py` exercises `/v1/admin/profiles`, which becomes session-guarded only in Task 7. Land Tasks 6 and 7 together; run this test at Task 7 Step 4.

- [ ] **Step 1: Add conftest fixtures**

Edit `apps/api/tests/conftest.py`. In the `client` fixture, after the other `monkeypatch.setenv(...)` lines and before `get_settings.cache_clear()`:

```python
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
```

Then add an `sso_client` fixture and an `admin_session` fixture at the end of the file:

```python
@pytest.fixture
def sso_client(database_url: str, async_database_url: str, monkeypatch) -> TestClient:
    """Like `client`, but with Google SSO configured (for /v1/auth flow tests)."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("AGENT_NAME", "usan-agent")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "http://testserver/v1/auth/callback")
    get_settings.cache_clear()

    test_engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        # follow_redirects off so the login 302 to Google is observable.
        yield TestClient(app, follow_redirects=False)
    finally:
        asyncio.run(_truncate_and_dispose(test_engine))
        get_settings.cache_clear()


async def _seed_admin_user_async(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:email, CAST(:role AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"email": email.lower(), "role": role},
            )
    finally:
        await engine.dispose()


@pytest.fixture
def admin_session(client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed an allow-listed admin and return cookies that authenticate as them.

    Sets the cookie on the shared `client` too, so tests can either rely on the
    client jar or pass `cookies=admin_session` per request.
    """
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole

    email = "admin@example.com"
    asyncio.run(_seed_admin_user_async(async_database_url, email, "admin"))
    token = issue_session(email, AdminRole.ADMIN, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return {SESSION_COOKIE_NAME: token}
```

(`text` is already imported in conftest; `create_async_engine`, `async_sessionmaker`, `NullPool`, `TestClient`, `get_settings`, `get_db`, `create_app`, `asyncio`, `TEST_SECRET` are all already imported/defined.)

- [ ] **Step 2: Write the failing test**

Create `apps/api/tests/test_require_admin_session.py`:

```python
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
```

- [ ] **Step 3: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_require_admin_session.py -v`
Expected: FAIL (`ImportError` for `AdminPrincipal`/`require_admin_session`, then — after Task 4-step deps land but before Task 7 — `401`/`403` mismatches because the route still uses operator-token). This is expected; the test goes green at Task 7 Step 4.

- [ ] **Step 4: Implement the dependencies**

Append to `apps/api/src/usan_api/auth.py`. Add to the imports at the top (keep the existing ones):

```python
from collections.abc import Callable, Coroutine
from dataclasses import dataclass

from fastapi import Cookie
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_session import SESSION_COOKIE_NAME, decode_session
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_users as admin_users_repo
```

Then append the dependencies to the end of `auth.py`:

```python
_COOKIE_AUTH = {"WWW-Authenticate": "Cookie"}


@dataclass(frozen=True)
class AdminPrincipal:
    """The authenticated admin operator for the current request."""

    email: str
    role: AdminRole


async def require_admin_session(
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    """Authenticate an admin operator from the session cookie.

    The cookie proves authentication (Google verified the human); the admin_users
    row provides current authorization + the live role. A removed/blocked operator
    is rejected immediately even while their cookie is unexpired. 401 (not 403) on a
    missing/invalid/expired cookie or a no-longer-allow-listed email.
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
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authorized",
            headers=_COOKIE_AUTH,
        )
    return AdminPrincipal(email=email, role=user.role)


def require_admin_role(
    required: AdminRole,
) -> Callable[..., Coroutine[Any, Any, AdminPrincipal]]:
    """Dependency factory: require at least `required` role (admin > viewer)."""

    async def _dep(principal: AdminPrincipal = Depends(require_admin_session)) -> AdminPrincipal:
        if required is AdminRole.ADMIN and principal.role is not AdminRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
        return principal

    return _dep
```

- [ ] **Step 5: Swap the actor source**

Replace the body of `apps/api/src/usan_api/admin_actor.py`:

```python
"""Actor identity for admin mutations.

P3 (Google SSO) wires this to the authenticated session: the audit log records the
operator's verified email.
"""

from fastapi import Depends

from usan_api.auth import AdminPrincipal, require_admin_session


def get_actor_email(principal: AdminPrincipal = Depends(require_admin_session)) -> str:
    return principal.email
```

- [ ] **Step 6: Verify import health (no cycle)**

Run: `cd apps/api && uv run python -c "import usan_api.admin_actor, usan_api.auth, usan_api.routers.admin_profiles; print('imports ok')"`
Expected: `imports ok` (auth.py must not import admin_actor; admin_actor imports auth — one direction only).

- [ ] **Step 7: Commit** (with Task 7, after its tests pass)

```bash
git add apps/api/src/usan_api/auth.py apps/api/src/usan_api/admin_actor.py apps/api/tests/conftest.py apps/api/tests/test_require_admin_session.py
git commit -m "feat(api): require_admin_session/role deps (DB-rechecked) + session audit actor + fixtures"
```

---

## Task 7: Swap `/v1/admin/profiles/*` to session auth + migrate its tests

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_profiles.py`
- Modify: `apps/api/tests/test_admin_profiles_api.py`
- Modify: `apps/api/tests/test_app_security.py` (comment only)

- [ ] **Step 1: Rewrite the router guard**

In `apps/api/src/usan_api/routers/admin_profiles.py`:

Change the imports — replace `from usan_api.auth import require_operator_token` with:

```python
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
```

Change the router to guard with the session dependency:

```python
router = APIRouter(
    prefix="/v1/admin/profiles",
    tags=["admin-profiles"],
    dependencies=[Depends(require_admin_session)],
)
```

Add the admin-role gate to each **mutating** route by adding this parameter (after the existing `actor: str = Depends(get_actor_email)`), to `create_profile`, `update_draft`, `publish`, `rollback`, `set_default`, and `archive`:

```python
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
```

The `GET` routes (`list_profiles`, `get_profile`, `list_versions`, `get_version`) keep only the router-level `require_admin_session` (a viewer can read). Example — `create_profile` becomes:

```python
@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileSummary)
async def create_profile(
    body: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ProfileSummary:
    ...
```

- [ ] **Step 2: Migrate `test_admin_profiles_api.py`**

Replace the whole file `apps/api/tests/test_admin_profiles_api.py` with the cookie-authenticated version:

```python
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import AdminAuditLog


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def _fetch_audit(async_database_url: str, action: str) -> AdminAuditLog | None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            result = await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == action))
            return result.scalars().first()
    finally:
        await engine.dispose()


def test_create_profile_returns_201(client, admin_session):
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 201
    body = r.json()
    assert body["published_version"] is None
    assert body["has_unpublished_draft"] is True


def test_create_profile_requires_session(client):
    # No session cookie: the management plane rejects the request.
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 401


def test_create_duplicate_name_returns_409(client, admin_session):
    name = _name()
    assert client.post("/v1/admin/profiles", json={"name": name}).status_code == 201
    r = client.post("/v1/admin/profiles", json={"name": name})
    assert r.status_code == 409


def test_create_with_unknown_clone_from_returns_404(client, admin_session):
    r = client.post(
        "/v1/admin/profiles",
        json={"name": _name(), "clone_from": str(uuid.uuid4())},
    )
    assert r.status_code == 404


def test_publish_then_list_versions(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "first"})
    assert r.status_code == 201
    assert r.json()["version"] == 1
    versions = client.get(f"/v1/admin/profiles/{pid}/versions").json()
    assert len(versions) == 1
    assert versions[0]["note"] == "first"


def test_edit_draft_then_get_reflects_change(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    detail = client.get(f"/v1/admin/profiles/{pid}").json()
    cfg = detail["draft_config"]
    cfg["prompts"]["greeting"] = "Hi there, this is your check-in."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert r.json()["draft_config"]["prompts"]["greeting"] == "Hi there, this is your check-in."


def test_draft_rejects_brace_in_prompt(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {name}"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 422


def test_rollback_creates_new_version(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Changed greeting here."
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"})
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={})
    assert r.status_code == 201
    assert r.json()["version"] == 3


def test_set_default_exclusive(client, admin_session):
    a = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    b = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    payload = {"direction": "outbound"}
    assert client.post(f"/v1/admin/profiles/{a}/set-default", json=payload).status_code == 200
    assert client.post(f"/v1/admin/profiles/{b}/set-default", json=payload).status_code == 200
    profiles = {p["id"]: p for p in client.get("/v1/admin/profiles").json()}
    assert profiles[a]["is_default_outbound"] is False
    assert profiles[b]["is_default_outbound"] is True


def test_archive_blocked_when_default_returns_409(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": "inbound"})
    r = client.post(f"/v1/admin/profiles/{pid}/archive", json={})
    assert r.status_code == 409


def test_get_missing_profile_returns_404(client, admin_session):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}")
    assert r.status_code == 404


def test_list_versions_unknown_profile_returns_404(client, admin_session):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}/versions")
    assert r.status_code == 404


def test_update_draft_unknown_profile_returns_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    r = client.put(f"/v1/admin/profiles/{uuid.uuid4()}/draft", json={"config": cfg})
    assert r.status_code == 404


def test_publish_unknown_profile_returns_404(client, admin_session):
    r = client.post(f"/v1/admin/profiles/{uuid.uuid4()}/publish", json={"note": "x"})
    assert r.status_code == 404


def test_rollback_unknown_version_returns_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/999", json={})
    assert r.status_code == 404


def test_get_version_returns_config_and_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    draft = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.get(f"/v1/admin/profiles/{pid}/versions/1")
    assert r.status_code == 200
    assert r.json()["config"] == draft
    missing = client.get(f"/v1/admin/profiles/{pid}/versions/999")
    assert missing.status_code == 404


def test_publish_records_audit_entry_with_session_actor(client, admin_session, async_database_url):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    assert r.status_code == 201
    entry = asyncio.run(_fetch_audit(async_database_url, "profile.publish"))
    assert entry is not None
    # Actor is now the authenticated operator's email, not the pre-SSO sentinel.
    assert entry.actor_email == "admin@example.com"
    assert entry.detail == {"version": 1}
```

- [ ] **Step 3: Fix the security-test comment**

In `apps/api/tests/test_app_security.py`, update the `test_admin_routes_are_rate_limited` comment to reflect session auth (assertions unchanged, still pass — the pre-limit requests now 401 from `require_admin_session`):

```python
def test_admin_routes_are_rate_limited(monkeypatch):
    # The /v1/admin/* management plane is session-guarded but must also be throttled
    # pre-auth, like the other operator routes. No session cookie is sent: the first
    # requests get 401 (auth), later ones 429 (rate limit).
```

- [ ] **Step 4: Run the migrated tests + the Task-6 deps test**

Run:
```bash
cd apps/api && uv run pytest tests/test_admin_profiles_api.py tests/test_require_admin_session.py tests/test_app_security.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_profiles.py apps/api/tests/test_admin_profiles_api.py apps/api/tests/test_app_security.py
git commit -m "feat(api): swap admin profiles plane from operator token to Google-SSO session auth"
```

---

## Task 8: Auth schemas + `/v1/auth/*` router

**Files:**
- Create: `apps/api/src/usan_api/schemas/auth.py`
- Create: `apps/api/src/usan_api/routers/auth.py`
- Modify: `apps/api/src/usan_api/main.py` (include router)
- Modify: `apps/api/src/usan_api/ratelimit.py` (throttle `/v1/auth/*`)
- Test: `apps/api/tests/test_auth_flow.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_auth_flow.py`:

```python
import asyncio
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import oauth
from usan_api.admin_session import SESSION_COOKIE_NAME, TX_COOKIE_NAME, issue_tx
from usan_api.settings import get_settings


async def _seed(async_database_url: str, email: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST('admin' AS admin_role), 'test') "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def test_login_redirects_to_google_and_sets_tx_cookie(sso_client):
    r = sso_client.get("/v1/auth/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert urlsplit(loc).netloc == "accounts.google.com"
    q = parse_qs(urlsplit(loc).query)
    assert q["code_challenge_method"] == ["S256"]
    assert TX_COOKIE_NAME in r.cookies


def test_login_503_when_sso_disabled(client):
    r = client.get("/v1/auth/login")
    assert r.status_code == 503


def test_callback_success_sets_session_and_redirects(sso_client, async_database_url, monkeypatch):
    asyncio.run(_seed(async_database_url, "alice@example.com"))

    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": "alice@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)

    tx = issue_tx("STATE123", "verifier", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "STATE123"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in r.cookies


def test_callback_denied_for_non_allowlisted(sso_client, monkeypatch):
    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": "stranger@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)

    tx = issue_tx("S", "v", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "S"})
    assert r.status_code == 403


def test_callback_state_mismatch_is_400(sso_client):
    tx = issue_tx("EXPECTED", "v", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "WRONG"})
    assert r.status_code == 400


def test_me_requires_session_then_returns_identity(client, admin_session):
    r = client.get("/v1/auth/me")
    assert r.status_code == 200
    assert r.json() == {"email": "admin@example.com", "role": "admin"}


def test_logout_clears_cookie(client, admin_session):
    r = client.post("/v1/auth/logout")
    assert r.status_code == 204
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_auth_flow.py -v`
Expected: FAIL (404s — the `/v1/auth/*` router is not registered yet).

- [ ] **Step 3: Implement the schemas**

Create `apps/api/src/usan_api/schemas/auth.py`:

```python
from pydantic import BaseModel, Field

# Minimal email regex avoids adding the email-validator dependency that EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class MeResponse(BaseModel):
    email: str
    role: str


class AdminUserOut(BaseModel):
    email: str
    role: str
    added_by: str | None = None


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")
```

- [ ] **Step 4: Implement the auth router**

Create `apps/api/src/usan_api/routers/auth.py`:

```python
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from usan_api import oauth
from usan_api.admin_session import (
    SESSION_COOKIE_NAME,
    TX_COOKIE_NAME,
    clear_session_cookie,
    clear_tx_cookie,
    decode_session,
    decode_tx,
    issue_session,
    issue_tx,
    set_session_cookie,
    set_tx_cookie,
)
from usan_api.auth import AdminPrincipal, require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.schemas.auth import MeResponse
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_SSO_DISABLED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SSO not configured"
)


@router.get("/login")
async def login(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """Begin Google OAuth: set the PKCE/state tx cookie and redirect to Google."""
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    state = oauth.new_state()
    verifier, challenge = oauth.new_pkce()
    url = oauth.build_authorization_url(settings, state=state, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    set_tx_cookie(resp, issue_tx(state, verifier, settings), settings)
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Complete Google OAuth: verify, allow-list check, issue the session cookie."""
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="oauth error")
    tx_cookie = request.cookies.get(TX_COOKIE_NAME)
    if not code or not state or not tx_cookie:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing oauth params")
    try:
        tx = decode_tx(tx_cookie, settings)
    except Exception as exc:  # noqa: BLE001 - any bad/tampered tx cookie is a 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid oauth transaction"
        ) from exc
    if not secrets.compare_digest(str(tx["state"]), state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state mismatch")

    try:
        id_token = await oauth.exchange_code(settings, code=code, code_verifier=str(tx["cv"]))
        claims: dict[str, Any] = oauth.verify_id_token(settings, id_token)
    except oauth.OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="authentication failed"
        ) from exc

    email = str(claims.get("email", "")).lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no email in token")
    user = await admin_users_repo.get_admin_user(db, email)
    if user is None:
        await admin_audit.record(
            db, actor_email=email, action="auth.denied", entity_type="admin_user", entity_id=email
        )
        await db.commit()
        logger.bind(email=email).warning("SSO login rejected: email not on allow-list")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized")

    await admin_audit.record(
        db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
    )
    await db.commit()
    resp = RedirectResponse(
        settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(resp, issue_session(email, user.role, settings), settings)
    clear_tx_cookie(resp, settings)
    return resp


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Clear the session cookie. Idempotent; safe without an active session."""
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(resp, settings)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        try:
            email = str(decode_session(cookie, settings)["sub"]).lower()
            logger.bind(email=email).info("admin logout")
        except Exception:  # noqa: BLE001 - logout must never fail on a bad cookie
            pass
    return resp


@router.get("/me", response_model=MeResponse)
async def me(principal: AdminPrincipal = Depends(require_admin_session)) -> MeResponse:
    return MeResponse(email=principal.email, role=principal.role.value)
```

- [ ] **Step 5: Register the router**

In `apps/api/src/usan_api/main.py`, add `auth` to the router import and include it next to `admin_profiles.router`:

```python
from usan_api.routers import admin_profiles, auth, calls, dnc, elders, runtime, tools, webhooks
```
```python
    app.include_router(auth.router)
```

- [ ] **Step 6: Throttle the auth plane**

In `apps/api/src/usan_api/ratelimit.py`, extend `_is_operator_route` so `/v1/auth/*` is rate-limited pre-auth:

```python
    if path.startswith("/v1/admin/") or path.startswith("/v1/auth/"):
        return True
```

(replace the existing `if path.startswith("/v1/admin/"): return True`). Update the module/function docstring to mention `/v1/auth/*`.

- [ ] **Step 7: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_auth_flow.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/schemas/auth.py apps/api/src/usan_api/routers/auth.py apps/api/src/usan_api/main.py apps/api/src/usan_api/ratelimit.py apps/api/tests/test_auth_flow.py
git commit -m "feat(api): /v1/auth Google SSO router (login, callback, logout, me) + rate-limit"
```

---

## Task 9: Admin-users management router

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_users.py`
- Modify: `apps/api/src/usan_api/main.py` (include router)
- Test: `apps/api/tests/test_admin_users_api.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_admin_users_api.py`:

```python
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
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_admin_users_api.py -v`
Expected: FAIL (404 — router not registered).

- [ ] **Step 3: Implement the router**

Create `apps/api/src/usan_api/routers/admin_users.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_users as repo
from usan_api.schemas.auth import AdminUserCreate, AdminUserOut

router = APIRouter(
    prefix="/v1/admin/admin-users",
    tags=["admin-users"],
    dependencies=[Depends(require_admin_session)],
)


def _to_out(user: AdminUser) -> AdminUserOut:
    return AdminUserOut(email=user.email, role=user.role.value, added_by=user.added_by)


@router.get("", response_model=list[AdminUserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[AdminUserOut]:
    return [_to_out(u) for u in await repo.list_admin_users(db)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AdminUserOut)
async def add_user(
    body: AdminUserCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> AdminUserOut:
    user = await repo.add_admin_user(db, email=body.email, role=AdminRole(body.role), added_by=actor)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="admin_user.add",
        entity_type="admin_user",
        entity_id=user.email,
        detail={"role": body.role},
    )
    await db.commit()
    return _to_out(user)


@router.delete("/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(
    email: str,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    removed = await repo.remove_admin_user(db, email)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="admin user not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="admin_user.remove",
        entity_type="admin_user",
        entity_id=email.lower(),
    )
    await db.commit()
```

- [ ] **Step 4: Register the router**

In `apps/api/src/usan_api/main.py`:

```python
from usan_api.routers import (
    admin_profiles,
    admin_users,
    auth,
    calls,
    dnc,
    elders,
    runtime,
    tools,
    webhooks,
)
```
```python
    app.include_router(admin_users.router)
```

- [ ] **Step 5: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_admin_users_api.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_users.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_users_api.py
git commit -m "feat(api): /v1/admin/admin-users CRUD (manage the SSO allow-list, audited)"
```

---

## Task 10: Bootstrap-seed the allow-list on startup

**Files:**
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_bootstrap_seed.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_bootstrap_seed.py`:

```python
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.session import get_db
from usan_api.main import create_app
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


async def _emails(async_database_url: str) -> set[str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(text("SELECT email FROM admin_users"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


def test_lifespan_seeds_bootstrap_emails(database_url, async_database_url, monkeypatch):
    async def _truncate():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("TRUNCATE admin_users RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(_truncate())

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "Founder@Example.com, ops@example.com")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app):  # entering the context runs lifespan startup
            pass
        emails = asyncio.run(_emails(async_database_url))
        assert {"founder@example.com", "ops@example.com"} <= emails
    finally:
        asyncio.run(engine.dispose())
        get_settings.cache_clear()
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `cd apps/api && uv run pytest tests/test_bootstrap_seed.py -v`
Expected: FAIL (the emails are not seeded — no startup hook yet).

- [ ] **Step 3: Implement the seeding in `lifespan`**

In `apps/api/src/usan_api/main.py`, add imports:

```python
from loguru import logger

from usan_api.db.session import dispose_engine, get_session_factory
from usan_api.repositories import admin_users as admin_users_repo
```

(merge the `db.session` import with the existing one; add `loguru` if not already imported.)

Add the helper above `lifespan`:

```python
async def _seed_admin_allowlist(settings: Settings) -> None:
    """Insert ADMIN_BOOTSTRAP_EMAILS into admin_users on startup (idempotent).

    Best-effort: a transient DB hiccup logs and does not crash the API (health stays
    up). Without at least one allow-listed email, nobody can log in via SSO.
    """
    emails = settings.bootstrap_emails_list
    if not emails:
        return
    try:
        async with get_session_factory()() as db:
            n = await admin_users_repo.seed_bootstrap(db, emails)
            await db.commit()
        if n:
            logger.info("Seeded {n} bootstrap admin user(s)", n=n)
    except Exception:  # noqa: BLE001 - startup must not crash on a seeding failure
        logger.exception("Failed to seed bootstrap admin allow-list")
```

In `lifespan`, right after `settings = get_settings()`:

```python
    await _seed_admin_allowlist(settings)
```

- [ ] **Step 4: Run it — expect PASS**

Run: `cd apps/api && uv run pytest tests/test_bootstrap_seed.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/main.py apps/api/tests/test_bootstrap_seed.py
git commit -m "feat(api): seed admin allow-list from ADMIN_BOOTSTRAP_EMAILS on startup"
```

---

## Task 11: Full verification + docs

**Files:**
- Modify: `apps/api/README.md` *(if present; else skip)*

- [ ] **Step 1: Lint, format, type-check, full suite**

```bash
cd apps/api
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```
Expected: ruff clean, format clean, mypy clean (CI runs mypy — see CLAUDE.md note), all tests pass. Common fixes: `E402` (keep imports at module top), `BLE001` (already `# noqa`-annotated where intentional), mypy on `_to_out` (the `AdminUser` import + typed signature already address it).

- [ ] **Step 2: Confirm the agent service is untouched**

```bash
cd services/agent && uv run pytest -q
```
Expected: PASS — P3 changes nothing in the agent (`apps/api ⊥ services/agent` holds).

- [ ] **Step 3: Document the new env (if `apps/api/README.md` exists)**

Add a short "Admin SSO (P3)" section listing the new env vars and noting infra wiring is P5:

```
GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI,
GOOGLE_OAUTH_HD (optional), ADMIN_BOOTSTRAP_EMAILS, ADMIN_SESSION_TTL_S (default 8h),
SESSION_COOKIE_SECURE (default true), ADMIN_POST_LOGIN_REDIRECT (default "/").
SSO is disabled until client id + secret + redirect URI are all set; until then the
admin plane requires a manually-minted session JWT. Secret Manager + compose + the
GCP redirect-URI registration are wired in P5 (infra).
```

- [ ] **Step 4: Commit (if docs changed)**

```bash
git add apps/api/README.md
git commit -m "docs(api): document admin SSO env + auth flow"
```

---

## Self-Review

**Spec coverage (design §9 + §14 P3):**
- `/v1/auth/login` + `/callback` (verify ID token: sig via JWKS, `aud`, `iss`, `email_verified`, optional `hd`) → Tasks 4, 8.
- Allow-list check + 403 + audit on reject → Task 8 (`auth.denied`).
- Own HttpOnly/Secure/SameSite=Strict session cookie signed with `JWT_SIGNING_KEY` (email + role + exp) → Tasks 3, 8.
- `/logout` clears cookie; `require_admin_session`/`require_admin_role` guards → Tasks 6, 8.
- `admin_users` allow-list + `ADMIN_BOOTSTRAP_EMAILS` seeding → Tasks 5, 10.
- Swap `/v1/admin/*` off `OPERATOR_API_KEY` → Task 7.
- Wire audit actor emails → Tasks 6 (actor swap) + 7 (verified by `test_publish_records_audit_entry_with_session_actor`).
- New env `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (+ redirect/hd/bootstrap/ttl/secure/redirect) → Task 2.
- `/v1/auth/*` (the fourth, cleanly separated auth mechanism) → Task 8. Allow-list management endpoints (design §8 `GET/POST/DELETE /admin-users`) → Task 9.

**Type consistency:** `AdminPrincipal(email, role: AdminRole)` produced by `require_admin_session`, consumed by `require_admin_role`, `/me`, `get_actor_email`. `issue_session(email, role: AdminRole, settings)` / `decode_session → dict` consistent across `admin_session.py`, `auth.py`, the router, and the `admin_session` fixture. `AdminRole` is the existing `db.base` enum (`admin`/`viewer`).

**Placeholder scan:** No TBDs. Each new module + test is shown verbatim; modifications give exact edits.

**Known sequencing note:** Task 6's `test_require_admin_session.py` hits `/v1/admin/profiles`, which only becomes session-guarded in Task 7 — run it at Task 7 Step 4 (the plan calls this out in Task 6).

**Out of scope (correctly deferred):** infra/compose/Caddy/Terraform/Secret-Manager + GCP redirect-URI registration (P5); the React SPA that consumes these endpoints (P4); per-elder/per-call profile assignment setters (a later admin-API slice, design §6.2).
