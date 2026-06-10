"""Two-layer SSRF defense for operator-registered webhook endpoint URLs.

The VM hosts a GCP metadata server at 169.254.169.254; operator-configured
egress URLs are a textbook SSRF vector.

Layer 1 — registration time (spec §8.1): ``validate_webhook_url`` runs in the
Pydantic schema validator on create and on every PATCH ``url``. HTTPS-only
(case-folded), no userinfo/fragment, length <= 2048, port 443/8443 only, and
the host must be a DNS name: ALL IP literals are rejected outright (public
included), as are inet_aton decoy literal forms (hex/octal/bare-decimal) that
``ipaddress`` does not parse but that resolve as IPs at connect time, the
internal-hostname denylist, and single-label hosts. The hostname is normalized
(lowercased, trailing dot stripped) before every check so
``metadata.google.internal.`` cannot slip past a suffix match.

Layer 2 — delivery time (spec §8.2): ``resolve_public_or_raise`` runs before
EVERY POST. It resolves all A/AAAA records and fails closed unless resolution
returned at least one address AND every address is globally routable
(IPv4-mapped IPv6 unwrapped first, so ``::ffff:169.254.169.254`` is judged as
its IPv4 self).

TOCTOU residual, stated in full breadth (spec §8.2): httpx has no first-class
resolve-then-connect IP pinning, so this phase is check-then-connect — a
malicious DNS server could rebind between our resolve and httpx's connect.
The metadata server specifically is additionally defended by its required
``Metadata-Flavor: Google`` header (our client never sends it), but that
defense is metadata-specific: a successful rebind could still land a POST on
other internal TLS services listening on 443/8443 (our own Caddy/API,
internal dashboards). Mitigations bounding the harm: (1) both layers run on
every attempt; (2) ``follow_redirects=False`` — a 3xx is a failure, never
followed; (3) ports restricted to 443/8443; (4) the request body is PHI-free
by construction (spec §6), so even a fully successful rebind exfiltrates only
opaque ids and bounded enums; (5) the POST carries no credentials of ours
(the HMAC secret is per-endpoint and signs, not authenticates-to, anything
internal). Closing the residual properly — a custom httpx transport that
connects to the vetted IP while sending SNI/Host for the original name — is
open Q2, elevated by review: do it before enabling delivery against
untrusted-DNS receivers at scale.
"""

import asyncio
import ipaddress
import re
from urllib.parse import urlsplit


class SsrfBlocked(Exception):
    """Delivery-time SSRF rejection.

    NAME IS LOAD-BEARING: ``type(exc).__name__`` is stored as
    ``webhook_deliveries.last_error='SsrfBlocked'`` (spec §5.3/§8.2).
    """


_MAX_URL_LEN = 2048
_DENY_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_DENY_EXACT = frozenset({"localhost"})
_ALLOWED_PORTS = frozenset({None, 443, 8443})  # open Q10 keeps 8443

# inet_aton accepts hex (0x7f000001), leading-zero octal (0177.0.0.1), and
# bare-decimal (2130706433) label forms that ipaddress.ip_address() does NOT
# parse; a host whose every dot-joined label matches one of these forms would
# resolve as an IP literal at connect time (spec §8.1 decoy literals).
_NUMERIC_LABEL = re.compile(r"0[xX][0-9a-fA-F]+|0[0-7]+|[0-9]+")


def _parses_as_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_webhook_url(url: str) -> str:
    """Registration-time SSRF gate (layer 1, spec §8.1).

    Raises ValueError with a stable message naming the failed rule;
    returns the url unchanged when valid.
    """
    if len(url) > _MAX_URL_LEN:
        raise ValueError("url longer than 2048 characters")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("url scheme must be https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not contain userinfo")
    if parts.fragment:
        raise ValueError("url must not contain a fragment")
    if parts.hostname is None:
        raise ValueError("url must contain a hostname")
    if parts.port not in _ALLOWED_PORTS:
        raise ValueError("url port must be 443 or 8443")
    # Normalize BEFORE all host checks: lowercase + strip trailing dot, so
    # "metadata.google.internal." cannot slip past the suffix match (§8.1).
    host = parts.hostname.lower().rstrip(".")
    if _parses_as_ip_literal(host):
        # ANY IPv4/IPv6 literal — public included; bracketed IPv6 arrives
        # unbracketed via urlsplit().hostname.
        raise ValueError("url host must be a DNS name, not an IP literal")
    if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError("url host must be a DNS name, not a numeric IP decoy literal")
    if host in _DENY_EXACT or host.endswith(_DENY_SUFFIXES):
        raise ValueError("url host is an internal hostname")
    if "." not in host:
        raise ValueError("url host must not be a single-label hostname")
    return url


async def _resolve(host: str) -> list[str]:
    """Resolution seam — delivery-time tests monkeypatch this.

    socket.gaierror/OSError from getaddrinfo PROPAGATES unwrapped — the
    delivery worker's except tuple owns the handling (plan executor note 4)
    and the type-name rule records last_error='gaierror'; collapsing it into
    SsrfBlocked would destroy the DNS-vs-policy diagnostic distinction.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def resolve_public_or_raise(host: str) -> None:
    """Delivery-time SSRF gate (layer 2, spec §8.2), run before EVERY POST.

    Fails closed: at least one address must resolve AND every resolved
    address must be globally routable.
    """
    addrs = await _resolve(host)
    if not addrs:
        # Fail-closed on empty resolution: all(...) over [] is vacuously true,
        # so non-emptiness must be checked first (spec §8.2 review fix).
        raise SsrfBlocked("DNS resolution returned no addresses")
    for addr in addrs:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            # Explicit IPv4-mapped unwrap: ::ffff:a.b.c.d is judged as its
            # IPv4 self regardless of runtime is_global semantics (spec §8.2).
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise SsrfBlocked("resolved address is not globally routable")
