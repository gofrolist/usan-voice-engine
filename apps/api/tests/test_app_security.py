from fastapi.testclient import TestClient

from usan_api.main import create_app
from usan_api.settings import get_settings

_BASE_ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _app_with_env(monkeypatch, **overrides):
    for k, v in {**_BASE_ENV, **overrides}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return create_app()


def test_docs_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DOCS_ENABLED", raising=False)
    app = _app_with_env(monkeypatch)
    try:
        client = TestClient(app)
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        # /health stays available regardless of docs.
        assert client.get("/health").status_code == 200
    finally:
        get_settings.cache_clear()


def test_docs_enabled_when_flag_set(monkeypatch):
    app = _app_with_env(monkeypatch, DOCS_ENABLED="true")
    try:
        client = TestClient(app)
        assert client.get("/openapi.json").status_code == 200
    finally:
        get_settings.cache_clear()


def _reset_limiter(app):
    limiter = app.state.limiter
    if hasattr(limiter, "reset"):
        limiter.reset()


def test_health_not_rate_limited_when_enabled(monkeypatch):
    # Internal/service routes (here /health) are never decorated, so they are not
    # throttled even when limiting is on — a busy call pipeline must not self-throttle.
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        _reset_limiter(app)
        client = TestClient(app)
        codes = [client.get("/health").status_code for _ in range(6)]
        assert all(c == 200 for c in codes)
    finally:
        get_settings.cache_clear()


def test_operator_route_throttled_when_enabled(monkeypatch):
    from usan_api.db.session import get_db

    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")

    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    try:
        _reset_limiter(app)
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"Authorization": "Bearer " + "o" * 32}
        url = "/v1/calls/00000000-0000-0000-0000-000000000000"
        codes = [client.get(url, headers=headers).status_code for _ in range(5)]
        assert 429 in codes  # operator routes are throttled past the per-client budget
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rate_limiting_disabled_passes_through(monkeypatch):
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="false")
    try:
        client = TestClient(app)
        assert app.state.limiter.enabled is False
        codes = [client.get("/health").status_code for _ in range(10)]
        assert all(c == 200 for c in codes)
    finally:
        get_settings.cache_clear()


def test_create_app_requires_operator_api_key(monkeypatch):
    for k, v in _BASE_ENV.items():
        if k == "OPERATOR_API_KEY":
            continue
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()
    raised = False
    try:
        create_app()
    except ValueError as exc:
        raised = "OPERATOR_API_KEY" in str(exc)
    finally:
        get_settings.cache_clear()
    assert raised
