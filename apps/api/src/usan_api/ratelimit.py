"""Pre-auth, per-client rate limiting for the operator/management plane.

A small ASGI middleware throttles only the externally reachable operator routes
(elders, DNC, outbound call enqueue/lookup) — BEFORE authentication, so an
unauthenticated flood is bounded too. Internal service routes (agent /v1/tools/*,
the inbound/outcome call hooks, LiveKit webhooks, and /health) are never matched,
so a busy call pipeline driving them from a few container IPs is never throttled.

State is a single in-memory fixed-window counter, so the configured limit is
per-process. That is correct for the current single-uvicorn-worker, single-API-
container deployment; move to a shared backend (e.g. Redis via limits' storage)
if the API is ever scaled horizontally.

Behind Caddy the real client arrives in X-Forwarded-For (Caddy overwrites it with
the direct peer — see infra/Caddyfile), so we key on its first hop, not the proxy.
"""

import time

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _client_key(request: Request) -> str:
    """The real client IP: the X-Forwarded-For first hop behind Caddy, else the peer."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # A header like ", 1.2.3.4" yields an empty first hop; don't collapse every
        # such request into one shared "" bucket — fall back to the real peer instead.
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return request.client.host if request.client else "unknown"


def _is_operator_route(method: str, path: str) -> bool:
    """True only for the six externally reachable operator/management endpoints.

    The internal /v1/calls routes (POST /inbound, POST /{id}/outcome) and every
    /v1/tools/*, /webhooks/*, and /health path fall through unthrottled.
    """
    if path.startswith("/v1/elders") or path.startswith("/v1/dnc"):
        return True
    if path == "/v1/calls" and method == "POST":  # enqueue_call
        return True
    # get_call: GET on a specific call; excludes POST /inbound and /{id}/outcome.
    return path.startswith("/v1/calls/") and method == "GET"


class OperatorRateLimitMiddleware:
    """Fixed-window per-client throttle on the operator plane; no-op when disabled."""

    def __init__(self, app: ASGIApp, *, limit: str, enabled: bool) -> None:
        self.app = app
        self.enabled = enabled
        self._limiter = FixedWindowRateLimiter(MemoryStorage())
        self._limit = parse(limit)

    def _retry_after(self, key: str) -> int:
        """Seconds until the client's window resets, for the Retry-After header (>=1)."""
        stats = self._limiter.get_window_stats(self._limit, "operator", key)
        return max(1, int(stats.reset_time - time.time()))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if _is_operator_route(request.method, request.url.path):
            key = _client_key(request)
            if not self._limiter.hit(self._limit, "operator", key):
                # RFC 9110 §15.5.30: a 429 SHOULD tell the client when to retry, so a
                # well-behaved caller backs off instead of hammering the window.
                response = JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(self._retry_after(key))},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
