import asyncio
import datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from usan_api import oauth
from usan_api.oauth import OAuthError
from usan_api.settings import Settings

_ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
    "GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://admin.example.com/v1/auth/callback",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_ENV, **overrides})  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _stub_certs(monkeypatch, rsa_key):
    """Make verify_id_token validate against our local RSA key, not Google's JWKS."""

    class _StubKey:
        def __init__(self, key):
            self.key = key

    class _StubJWKS:
        def get_signing_key_from_jwt(self, token):
            return _StubKey(rsa_key.public_key())

    monkeypatch.setattr(oauth, "_certs", lambda: _StubJWKS())


def _id_token(rsa_key, **claims) -> str:
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "iss": "https://accounts.google.com",
        "aud": "cid.apps.googleusercontent.com",
        "email": "alice@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + datetime.timedelta(minutes=5),
    }
    payload.update(claims)
    return jwt.encode(payload, rsa_key, algorithm="RS256")


def test_valid_id_token_passes(rsa_key):
    claims = oauth.verify_id_token(_settings(), _id_token(rsa_key))
    assert claims["email"] == "alice@example.com"


def test_email_not_verified_rejected(rsa_key):
    with pytest.raises(OAuthError):
        oauth.verify_id_token(_settings(), _id_token(rsa_key, email_verified=False))


def test_bad_issuer_rejected(rsa_key):
    with pytest.raises(OAuthError):
        oauth.verify_id_token(_settings(), _id_token(rsa_key, iss="https://evil.example.com"))


def test_wrong_audience_rejected(rsa_key):
    with pytest.raises(OAuthError):
        oauth.verify_id_token(_settings(), _id_token(rsa_key, aud="someone-else"))


def test_expired_rejected(rsa_key):
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    with pytest.raises(OAuthError):
        oauth.verify_id_token(_settings(), _id_token(rsa_key, exp=past))


def test_hd_mismatch_rejected(rsa_key):
    s = _settings(GOOGLE_OAUTH_HD="usan.example")
    with pytest.raises(OAuthError):
        oauth.verify_id_token(s, _id_token(rsa_key, hd="other.example"))


def test_hd_match_passes(rsa_key):
    s = _settings(GOOGLE_OAUTH_HD="usan.example")
    claims = oauth.verify_id_token(s, _id_token(rsa_key, hd="usan.example"))
    assert claims["hd"] == "usan.example"


def test_exchange_code_missing_id_token_raises(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "x"}  # no id_token

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(oauth.httpx, "AsyncClient", _Client)
    with pytest.raises(OAuthError):
        asyncio.run(oauth.exchange_code(_settings(), code="c", code_verifier="v"))
