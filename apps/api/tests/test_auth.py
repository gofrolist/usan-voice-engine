import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from usan_api.auth import require_service_token
from usan_api.settings import get_settings

SECRET = "s" * 32


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(claims: dict = Depends(require_service_token)) -> dict:
        return {"call_id": claims["call_id"]}

    return app


@pytest.fixture
def auth_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", SECRET)
    get_settings.cache_clear()
    try:
        yield TestClient(_app())
    finally:
        # Don't let this fixture's fake DATABASE_URL bleed into later tests.
        get_settings.cache_clear()


def _token(secret=SECRET, *, call_id="abc", exp_delta=300, **extra) -> str:
    now = int(time.time())
    claims = {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + exp_delta}
    claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_valid_token_accepted(auth_client):
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json()["call_id"] == "abc"


def test_missing_token_401(auth_client):
    assert auth_client.get("/protected").status_code == 401


def test_wrong_secret_401(auth_client):
    bad = _token(secret="x" * 32)
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_expired_token_401(auth_client):
    expired = _token(exp_delta=-10)
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_missing_call_id_claim_401(auth_client):
    now = int(time.time())
    token = jwt.encode({"sub": "usan-agent", "exp": now + 300}, SECRET, algorithm="HS256")
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
