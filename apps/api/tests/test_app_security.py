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


def test_operator_route_throttled_before_auth(monkeypatch):
    # The limiter runs in middleware, before the operator-token dependency, so even
    # an unauthenticated flood is bounded. No auth header is sent: the first requests
    # get 401 (auth), later ones 429 (rate limit) — proving the throttle is pre-auth.
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = "/v1/calls/00000000-0000-0000-0000-000000000000"
        codes = [client.get(url).status_code for _ in range(5)]
        assert 429 in codes
        assert codes.index(429) >= 2  # the first two (the budget) were not throttled
    finally:
        get_settings.cache_clear()


def test_admin_routes_are_rate_limited(monkeypatch):
    # The /v1/admin/* management plane is session-guarded but must also be throttled
    # pre-auth, like the other operator routes. No session cookie is sent: the first
    # requests get 401 (auth), later ones 429 (rate limit).
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        codes = [client.get("/v1/admin/profiles").status_code for _ in range(5)]
        assert 429 in codes
        assert codes.index(429) >= 2  # the first two (the budget) were not throttled
    finally:
        get_settings.cache_clear()


def test_rate_limit_429_includes_retry_after(monkeypatch):
    # RFC 9110 §15.5.30: the 429 must tell a well-behaved client when to retry.
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = "/v1/calls/00000000-0000-0000-0000-000000000000"
        throttled = next(
            (r for r in (client.get(url) for _ in range(5)) if r.status_code == 429), None
        )
        assert throttled is not None
        retry_after = throttled.headers.get("Retry-After")
        assert retry_after is not None
        assert retry_after.isdigit()
        assert int(retry_after) >= 1
    finally:
        get_settings.cache_clear()


def test_internal_routes_not_rate_limited(monkeypatch):
    # Agent/service and health routes must never be throttled (high volume from a few
    # container IPs). Unauthenticated, tool routes return 401 from their JWT guard.
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        tool_codes = [client.post("/v1/tools/end_call", json={}).status_code for _ in range(6)]
        assert 429 not in tool_codes
        health_codes = [client.get("/health").status_code for _ in range(6)]
        assert all(c == 200 for c in health_codes)
    finally:
        get_settings.cache_clear()


def test_rate_limiting_disabled_passes_through(monkeypatch):
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="false")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = "/v1/calls/00000000-0000-0000-0000-000000000000"
        codes = [client.get(url).status_code for _ in range(8)]
        assert 429 not in codes  # disabled: these are 401 from auth, never throttled
    finally:
        get_settings.cache_clear()


def test_is_operator_route_matches_schedules_and_batches():
    from usan_api.ratelimit import _is_operator_route

    assert _is_operator_route("POST", "/v1/schedules")
    assert _is_operator_route("GET", "/v1/schedules/123")
    assert _is_operator_route("PATCH", "/v1/schedules/123")
    assert _is_operator_route("POST", "/v1/batches")
    assert _is_operator_route("POST", "/v1/batches/123/cancel")
    assert _is_operator_route("GET", "/v1/batches")


def test_schedules_and_batches_routes_throttled_pre_auth(monkeypatch):
    # Mirror of test_admin_routes_are_rate_limited: the schedule/batch operator
    # routes must be throttled pre-auth too. No auth header is sent; the budget is
    # per-client (not per-path), so after two /v1/schedules requests the /v1/batches
    # requests are throttled as well.
    app = _app_with_env(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT="2/minute")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        codes = [client.get("/v1/schedules").status_code for _ in range(3)]
        codes += [client.post("/v1/batches", json={}).status_code for _ in range(2)]
        assert 429 in codes
        assert codes.index(429) >= 2  # the first two (the budget) were not throttled
        assert codes[-1] == 429  # the shared budget also covers /v1/batches
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
