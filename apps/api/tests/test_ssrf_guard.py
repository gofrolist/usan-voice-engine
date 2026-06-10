"""SSRF guard matrices (spec §8.1 registration / §8.2 delivery / §10.4 strategy).

Layer 1 (validate_webhook_url) is a pure-function rejection matrix; layer 2
(resolve_public_or_raise) is tested through the monkeypatchable `_resolve`
seam, plus one real-getaddrinfo test against localhost (deterministic on any
host) so the `info[4][0]` extraction is exercised at least once.
"""

import socket

import pytest

from usan_api import ssrf_guard
from usan_api.ssrf_guard import SsrfBlocked, resolve_public_or_raise, validate_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        # scheme
        "http://hooks.example.com/x",
        # IP literals — rejected outright, public included (§8.1: the rule is
        # "host must be a DNS name", NOT "host must not be private"; an
        # is_private-based implementation must fail the two public literals)
        "https://192.168.1.1/",
        "https://[::1]/",
        "https://93.184.216.34/",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/",
        # bracketed IPv6 decoys
        "https://[fe80::1]/",
        "https://[fd00::1]/",
        "https://[::ffff:169.254.169.254]/",
        # inet_aton decoy literal forms (bare decimal / hex / dotted hex / octal)
        "https://2130706433/",
        "https://0x7f000001/",
        "https://0x7f.0.0.1/",
        "https://0177.0.0.1/",
        # hostname denylist (normalized: lowercase, trailing dot stripped)
        "https://localhost/x",
        "https://foo.localhost/",
        "https://printer.local/",
        "https://foo.internal/",
        "https://metadata.google.internal/",
        "https://metadata.google.internal./computeMetadata",
        "https://METADATA.GOOGLE.INTERNAL/",
        "https://host.home.arpa/",
        # single-label host
        "https://intranet/",
        # userinfo / fragment / port / length
        "https://user:pass@hooks.example.com/",
        "https://hooks.example.com/x#frag",
        "https://hooks.example.com:8080/",
        "https://hooks.example.com/" + "a" * 2050,
    ],
)
def test_validate_webhook_url_rejects(url: str):
    with pytest.raises(ValueError):  # noqa: PT011 - each message names its rule
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://Hooks.Example.com/path",  # scheme case-fold
        "https://hooks.example.com:8443/path?x=y",
        "https://hooks.example.com:443/",
        "https://hooks.example.com/hook?token=abc",
        # Trailing dot on an allowed host normalizes via rstrip(".") and is
        # ACCEPTED — pinned so the behavior is a decision, not an accident.
        "https://hooks.example.com./",
    ],
)
def test_validate_webhook_url_accepts(url: str):
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "addrs",
    [
        ["169.254.169.254"],  # GCP metadata server
        ["10.0.0.5"],  # RFC1918
        ["::1"],  # IPv6 loopback
        ["fd00::1"],  # ULA
        ["fe80::1"],  # link-local
        ["100.64.0.1"],  # CGNAT shared space
        ["::ffff:169.254.169.254"],  # IPv4-mapped unwrap
        ["::ffff:10.0.0.1"],  # IPv4-mapped unwrap
        ["93.184.216.34", "10.0.0.5"],  # mixed — EVERY address must be global
        [],  # empty resolution — fail-closed (§8.2)
    ],
)
async def test_resolve_public_or_raise_rejects(monkeypatch: pytest.MonkeyPatch, addrs: list[str]):
    async def _fake_resolve(host: str) -> list[str]:
        return addrs

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    with pytest.raises(SsrfBlocked) as excinfo:
        await resolve_public_or_raise("hooks.example.com")
    # The last_error contract: webhook_deliveries.last_error stores the
    # exception type name, which must equal the spec-pinned 'SsrfBlocked'.
    assert type(excinfo.value).__name__ == "SsrfBlocked"


@pytest.mark.parametrize(
    "addrs",
    [
        ["93.184.216.34"],
        ["2606:2800:220:1:248:1893:25c8:1946"],
    ],
)
async def test_resolve_public_or_raise_accepts(monkeypatch: pytest.MonkeyPatch, addrs: list[str]):
    async def _fake_resolve(host: str) -> list[str]:
        return addrs

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    await resolve_public_or_raise("hooks.example.com")  # must not raise


async def test_resolve_propagates_gaierror(monkeypatch: pytest.MonkeyPatch):
    # DNS resolution failure propagates UNWRAPPED — not swallowed, not
    # converted to SsrfBlocked. The delivery worker's except tuple owns it
    # (executor note 4) and the type-name rule yields last_error='gaierror';
    # collapsing into SsrfBlocked would destroy the DNS-vs-policy diagnostic
    # distinction.
    async def _fake_resolve(host: str) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    with pytest.raises(socket.gaierror):
        await resolve_public_or_raise("nxdomain.example.com")


async def test_resolve_real_localhost_blocked():
    # No monkeypatch: exercises the real getaddrinfo extraction info[4][0],
    # which every other delivery-time test bypasses; loopback resolution is
    # deterministic on any host.
    with pytest.raises(SsrfBlocked):
        await resolve_public_or_raise("localhost")
