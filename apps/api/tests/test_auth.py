import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from usan_api.auth import require_operator_token, require_service_token, require_worker_token
from usan_api.settings import get_settings

SECRET = "s" * 32
OPERATOR_KEY = "o" * 32


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(claims: dict = Depends(require_service_token)) -> dict:
        return {"call_id": claims["call_id"]}

    @app.get("/worker")
    def worker(claims: dict = Depends(require_worker_token)) -> dict:
        return {"sub": claims.get("sub")}

    @app.get("/operator")
    def operator(_: None = Depends(require_operator_token)) -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def auth_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", SECRET)
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
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


def _worker_token_str(secret: str = SECRET, *, exp_delta: int = 300) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + exp_delta}, secret, algorithm="HS256"
    )


def test_worker_token_without_call_id_accepted(auth_client):
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {_worker_token_str()}"})
    assert r.status_code == 200


def test_worker_token_missing_401(auth_client):
    assert auth_client.get("/worker").status_code == 401


def test_worker_token_wrong_secret_401(auth_client):
    bad = _worker_token_str(secret="x" * 32)
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_worker_token_expired_401(auth_client):
    expired = _worker_token_str(exp_delta=-10)
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_worker_token_accepts_call_scoped_token(auth_client):
    # A call-scoped token (with call_id) is also valid — we only require exp.
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200


def test_operator_token_accepted(auth_client):
    r = auth_client.get("/operator", headers={"Authorization": f"Bearer {OPERATOR_KEY}"})
    assert r.status_code == 200


def test_operator_token_missing_401(auth_client):
    assert auth_client.get("/operator").status_code == 401


def test_operator_token_wrong_key_401(auth_client):
    r = auth_client.get("/operator", headers={"Authorization": "Bearer " + "x" * 32})
    assert r.status_code == 401


def test_operator_token_non_bearer_scheme_401(auth_client):
    r = auth_client.get("/operator", headers={"Authorization": f"Basic {OPERATOR_KEY}"})
    assert r.status_code == 401


def test_operator_401_includes_www_authenticate(auth_client):
    # RFC 7235 §3.1: every 401 must carry a WWW-Authenticate challenge.
    missing = auth_client.get("/operator")
    assert missing.status_code == 401
    assert missing.headers.get("WWW-Authenticate") == "Bearer"
    wrong = auth_client.get("/operator", headers={"Authorization": "Bearer " + "x" * 32})
    assert wrong.status_code == 401
    assert wrong.headers.get("WWW-Authenticate") == "Bearer"


def test_service_and_worker_401_include_www_authenticate(auth_client):
    for path in ("/protected", "/worker"):
        r = auth_client.get(path)
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_agent_verifiers_reject_cookie_token_types(auth_client):
    # admin_session / oauth_tx / oauth_invite JWTs are signed with the same
    # JWT_SIGNING_KEY as agent tokens; the service & worker verifiers must reject them by
    # `typ` so a session/invite cookie cannot be replayed as a Bearer worker/service token.
    for typ in ("admin_session", "oauth_tx", "oauth_invite"):
        tok = _token(typ=typ)  # carries call_id + exp, so only the typ check can reject it
        assert (
            auth_client.get("/protected", headers={"Authorization": f"Bearer {tok}"}).status_code
            == 401
        )
        assert (
            auth_client.get("/worker", headers={"Authorization": f"Bearer {tok}"}).status_code
            == 401
        )
