"""Real client IP extraction, shared by rate limiting and PHI access audit logs.

Behind Caddy the socket peer is the proxy container (and, once Cloudflare-proxied,
the Cloudflare edge), not the operator. Caddy overwrites ``X-Forwarded-For`` with
the true client — ``CF-Connecting-IP`` behind Cloudflare (see ``infra/Caddyfile``:
``header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}``) — so its
first hop is the real external client. Both the rate-limit key and the audit trail
must use that, not ``request.client.host`` — otherwise every request collapses
into the single proxy/edge IP.
"""

from starlette.requests import Request


def client_ip(request: Request, trusted_proxies: frozenset[str] = frozenset()) -> str:
    """The real client IP: the X-Forwarded-For first hop behind Caddy, else the peer.

    ``trusted_proxies`` gates whether the X-Forwarded-For header is honored:

    - **empty** (the default): legacy behavior — trust the XFF first hop
      unconditionally. Correct in prod because Caddy + Cloudflare AOP mTLS are the only
      path to the API, so the header is always proxy-written.
    - **non-empty** (the rate-limit middleware passes ``settings.trusted_proxy_set``):
      trust XFF ONLY when the immediate peer (``request.client.host``) is a configured
      proxy; otherwise a direct/co-resident caller is spoofing XFF, so the real socket
      peer is used instead — closing the rate-limit-bypass / forged-audit vector for any
      future direct exposure.

    Pure (no global-state read) so it is trivially unit-testable.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # A header like ", 1.2.3.4" yields an empty first hop; don't collapse every
        # such request into one shared bucket/audit entry — fall back to the peer.
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            peer = request.client.host if request.client else None
            if not trusted_proxies or peer in trusted_proxies:
                return candidate
    return request.client.host if request.client else "unknown"
