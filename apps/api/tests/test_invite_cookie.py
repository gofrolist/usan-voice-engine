import jwt
import pytest

from usan_api.admin_session import (
    INVITE_COOKIE_NAME,
    decode_invite,
    issue_invite,
    issue_tx,
)
from usan_api.settings import Settings

# INVITE_COOKIE_NAME is imported to assert the module exposes it (the cookie helpers
# reference it); reference it once here so the linter keeps the import meaningful.
assert INVITE_COOKIE_NAME == "admin_invite_tx"

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


def test_invite_cookie_roundtrip():
    s = _settings()
    token = issue_invite("the-token", s)
    claims = decode_invite(token, s)
    assert claims["invite_token"] == "the-token"


def test_decode_invite_rejects_wrong_type():
    s = _settings()
    tx = issue_tx("state", "verifier", s)
    with pytest.raises(jwt.PyJWTError):
        decode_invite(tx, s)
