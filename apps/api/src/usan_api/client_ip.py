"""Real client IP extraction, shared by rate limiting and PHI access audit logs.

Behind Caddy the socket peer is the proxy container, not the operator. Caddy
overwrites ``X-Forwarded-For`` with the direct, non-spoofable client (see
``infra/Caddyfile``: ``header_up X-Forwarded-For {remote_host}``), so its first
hop is the true external client. Both the rate-limit key and the audit trail
must use that, not ``request.client.host`` — otherwise every request collapses
into the single proxy IP.
"""

from starlette.requests import Request


def client_ip(request: Request) -> str:
    """The real client IP: the X-Forwarded-For first hop behind Caddy, else the peer."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # A header like ", 1.2.3.4" yields an empty first hop; don't collapse every
        # such request into one shared bucket/audit entry — fall back to the peer.
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return request.client.host if request.client else "unknown"
