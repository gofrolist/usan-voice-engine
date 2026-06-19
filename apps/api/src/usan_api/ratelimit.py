"""Pre-auth, per-client rate limiting for the operator/management plane.

A small ASGI middleware throttles only the externally reachable operator routes
(contacts, DNC, outbound call enqueue/lookup, and the /v1/admin/* management plane)
— BEFORE authentication, so an unauthenticated flood is bounded too. Internal
service routes (agent /v1/tools/*,
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

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from usan_api.client_ip import client_ip


def _is_operator_route(method: str, path: str) -> bool:
    """True for the externally reachable operator/management endpoints.

    Covers the operator data-plane routes (contacts, DNC, outbound call
    enqueue/lookup, call schedules, call batches, webhook endpoints/deliveries)
    plus the entire /v1/admin/* management plane (the operator-token-guarded
    admin UI backend).

    The internal /v1/calls routes (POST /inbound, POST /{id}/outcome) and every
    /v1/tools/*, /webhooks/*, and /health path fall through unthrottled.
    """
    if path.startswith("/v1/admin/") or path.startswith("/v1/auth/"):
        return True
    if path.startswith("/v1/contacts") or path.startswith("/v1/dnc"):
        return True
    if path.startswith("/v1/schedules") or path.startswith("/v1/batches"):
        return True
    if path.startswith("/v1/webhook-endpoints") or path.startswith("/v1/webhook-deliveries"):
        return True
    if path == "/v1/calls" and method == "POST":  # enqueue_call
        return True
    # get_call: GET on a specific call; excludes POST /inbound and /{id}/outcome.
    return path.startswith("/v1/calls/") and method == "GET"


# Per-call_id fixed-window limiter for the agent tool plane (/v1/tools/*). Separate from
# the operator middleware: those routes are intentionally NOT IP-throttled (a busy call
# pipeline must flow), but a runaway/looping or hijacked agent token still needs a ceiling
# so it can't flood DB writes / SMS / family notifications. Keyed on the call_id claim.
_tool_call_limiter = FixedWindowRateLimiter(MemoryStorage())


def tool_call_within_limit(call_id: str, limit: str) -> bool:
    """Record one tool call for ``call_id``; return False once the window is exhausted.

    In-memory + per-process (like the operator limiter): correct for the single-worker
    deployment, and a hijacked token's blast radius is bounded per worker regardless.
    """
    return _tool_call_limiter.hit(parse(limit), "tool_call", call_id)


class OperatorRateLimitMiddleware:
    """Fixed-window per-client throttle on the operator plane; no-op when disabled."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: str,
        enabled: bool,
        auth_limit: str,
        trusted_proxies: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.enabled = enabled
        self._limiter = FixedWindowRateLimiter(MemoryStorage())
        self._limit = parse(limit)
        # Tighter, separate bucket for the pre-auth /v1/auth/* endpoints so credential
        # stuffing / OAuth-state probing can't piggy-back on a budget that bulk operator
        # reads have already spent (distinct namespace => independent counter).
        self._auth_limit = parse(auth_limit)
        # When configured, only honor X-Forwarded-For from these proxy peers — so a
        # spoofed XFF can't forge the rate-limit key (the unauthenticated bypass vector).
        self._trusted_proxies = trusted_proxies

    def _limit_for(self, path: str) -> tuple[RateLimitItem, str]:
        """(limit, bucket-namespace) for a path: the tight auth bucket vs the operator one."""
        if path.startswith("/v1/auth/"):
            return self._auth_limit, "auth"
        return self._limit, "operator"

    def _retry_after(self, limit: RateLimitItem, namespace: str, key: str) -> int:
        """Seconds until the client's window resets, for the Retry-After header (>=1)."""
        stats = self._limiter.get_window_stats(limit, namespace, key)
        return max(1, int(stats.reset_time - time.time()))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if _is_operator_route(request.method, path):
            key = client_ip(request, self._trusted_proxies)
            limit, namespace = self._limit_for(path)
            if not self._limiter.hit(limit, namespace, key):
                # RFC 9110 §15.5.30: a 429 SHOULD tell the client when to retry, so a
                # well-behaved caller backs off instead of hammering the window.
                response = JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(self._retry_after(limit, namespace, key))},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
