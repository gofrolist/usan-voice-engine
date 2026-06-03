"""Per-client rate limiting for the operator/management plane (slowapi).

Only the externally reachable operator routes (elders, DNC, outbound call
enqueue/lookup) are decorated with ``limiter.limit``. Internal service routes —
agent tool calls (``/v1/tools/*``), the inbound/outcome call hooks, LiveKit
webhooks, and ``/health`` — are deliberately left undecorated, so a busy call
pipeline (which drives those at high frequency from a small set of container IPs)
is never throttled.

Behind the Caddy reverse proxy every external request arrives from Caddy's own
container IP, so keying on the socket peer would collapse all callers into one
bucket. Caddy is configured to overwrite ``X-Forwarded-For`` with the real client
(see infra/Caddyfile), so we key on its first hop instead.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from usan_api.settings import get_settings


def _client_key(request: Request) -> str:
    """Rate-limit key: the real client IP (X-Forwarded-For first hop behind Caddy)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Caddy overwrites XFF with the direct peer, so the first hop is the real,
        # non-spoofable client address.
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


def operator_limit() -> str:
    """The active per-client limit for operator routes (re-read per request)."""
    return get_settings().rate_limit_default


# Module-level singleton so route decorators can reference it at import time. The
# enabled flag and storage are (re)configured per app build in main.create_app.
limiter = Limiter(key_func=_client_key, default_limits=[])
