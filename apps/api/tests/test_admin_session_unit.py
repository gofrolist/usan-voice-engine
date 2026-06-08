import base64
import datetime
import hashlib
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from usan_api import oauth
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


def test_pkce_pair_is_valid():
    verifier, challenge = oauth.new_pkce()
    # RFC 7636: verifier 43-128 chars; challenge is url-safe base64 of SHA256(verifier).
    assert 43 <= len(verifier) <= 128
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
