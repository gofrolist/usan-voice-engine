"""T046 — the rate limiter covers the compat surface with its OWN per-key bucket,
separate from the operator/auth budgets (feature 003, FR-054).

The migrated CRM runs high call volume on its key; it must be throttled (a runaway
key can't flood the engine) but with a dedicated, elevated bucket keyed per Bearer
key — never sharing or exhausting the operator plane, and never starving another key.
"""

from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from usan_api.ratelimit import OperatorRateLimitMiddleware, _is_compat_route


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


def _client(compat_limit: str = "2/minute") -> TestClient:
    # Huge operator/auth budgets so any throttling we observe is the compat bucket.
    mw = OperatorRateLimitMiddleware(
        _ok_app,
        limit="1000/minute",
        enabled=True,
        auth_limit="1000/minute",
        compat_limit=compat_limit,
    )
    return TestClient(mw)


_KEY_A = {"Authorization": "Bearer keyAAAAAA_secret_rest"}
_KEY_B = {"Authorization": "Bearer keyBBBBBB_secret_rest"}


def test_is_compat_route_classification():
    for path in (
        "/create-agent",
        "/v2/create-phone-call",
        "/v3/list-calls",
        "/create-batch-call",
        "/list-voices",
        "/get-concurrency",
        "/create-knowledge-base",  # an out-of-scope stub is still a compat request
    ):
        assert _is_compat_route(path) is True, path
    for path in (
        "/v1/contacts",
        "/v1/admin/compat-keys",
        "/health",
        "/metrics",
        "/openapi.json",
        "/compat/openapi.json",  # docs surface, not an API call
        "/",
    ):
        assert _is_compat_route(path) is False, path


def test_compat_surface_throttled_by_its_own_bucket():
    client = _client(compat_limit="2/minute")
    assert client.post("/create-agent", headers=_KEY_A).status_code == 200
    assert client.post("/create-agent", headers=_KEY_A).status_code == 200
    blocked = client.post("/create-agent", headers=_KEY_A)  # 3rd > limit of 2
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_compat_bucket_is_per_key_not_shared():
    client = _client(compat_limit="2/minute")
    assert client.post("/create-agent", headers=_KEY_A).status_code == 200
    assert client.post("/create-agent", headers=_KEY_A).status_code == 200
    assert client.post("/create-agent", headers=_KEY_A).status_code == 429  # A exhausted
    # A DIFFERENT key has its own independent budget — one CRM key can't starve another.
    assert client.post("/create-agent", headers=_KEY_B).status_code == 200


def test_compat_exhaustion_does_not_touch_the_operator_budget():
    client = _client(compat_limit="2/minute")
    for _ in range(3):
        client.post("/create-agent", headers=_KEY_A)  # drive the compat bucket to 429
    # An operator-plane route still flows: it lives in a separate (operator) bucket.
    assert client.post("/v1/contacts", headers=_KEY_A).status_code == 200


def test_rate_limit_disabled_is_a_passthrough():
    mw = OperatorRateLimitMiddleware(
        _ok_app, limit="1/minute", enabled=False, auth_limit="1/minute", compat_limit="1/minute"
    )
    client = TestClient(mw)
    for _ in range(5):
        assert client.post("/create-agent", headers=_KEY_A).status_code == 200
