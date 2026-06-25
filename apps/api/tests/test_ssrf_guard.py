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
    ("url", "match"),
    [
        # scheme
        ("http://hooks.example.com/x", "scheme must be https"),
        # IP literals — rejected outright, public included (§8.1: the rule is
        # "host must be a DNS name", NOT "host must not be private"; an
        # is_private-based implementation must fail the two public literals)
        ("https://192.168.1.1/", "not an IP literal"),
        ("https://[::1]/", "not an IP literal"),
        ("https://93.184.216.34/", "not an IP literal"),
        ("https://[2606:2800:220:1:248:1893:25c8:1946]/", "not an IP literal"),
        # bracketed IPv6 decoys
        ("https://[fe80::1]/", "not an IP literal"),
        ("https://[fd00::1]/", "not an IP literal"),
        ("https://[::ffff:169.254.169.254]/", "not an IP literal"),
        # inet_aton decoy literal forms (bare decimal / hex / dotted hex /
        # octal). The match pins the DECOY branch: the two single-label forms
        # (bare-decimal, plain hex) would otherwise be shadowed by the
        # single-label rejection and the decoy branch could silently rot.
        ("https://2130706433/", "numeric IP decoy literal"),
        ("https://0x7f000001/", "numeric IP decoy literal"),
        ("https://0x7f.0.0.1/", "numeric IP decoy literal"),
        ("https://0177.0.0.1/", "numeric IP decoy literal"),
        # hostname denylist (normalized: lowercase, trailing dot stripped)
        ("https://localhost/x", "internal hostname"),
        ("https://foo.localhost/", "internal hostname"),
        ("https://printer.local/", "internal hostname"),
        ("https://foo.internal/", "internal hostname"),
        ("https://metadata.google.internal/", "internal hostname"),
        ("https://metadata.google.internal./computeMetadata", "internal hostname"),
        ("https://METADATA.GOOGLE.INTERNAL/", "internal hostname"),
        ("https://host.home.arpa/", "internal hostname"),
        # single-label host
        ("https://intranet/", "single-label hostname"),
        # userinfo / fragment / port / length
        ("https://user:pass@hooks.example.com/", "must not contain userinfo"),
        ("https://hooks.example.com/x#frag", "must not contain a fragment"),
        ("https://hooks.example.com:8080/", "port must be 443 or 8443"),
        ("https://hooks.example.com/" + "a" * 2050, "longer than 2048 characters"),
    ],
)
def test_validate_webhook_url_rejects(url: str, match: str):
    # match= pins each row to ITS rule's stable message — a bare ValueError
    # would let one rejection branch shadow another (review fix).
    with pytest.raises(ValueError, match=match):
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


async def test_resolve_public_or_raise_returns_validated_addrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pin path needs the validated IPs back so the caller connects to a vetted
    # address rather than letting httpx re-resolve (TOCTOU close, §8.2).
    async def _fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    addrs = await resolve_public_or_raise("hooks.example.com")
    assert addrs == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize(
    ("url", "ip", "expected_url", "expected_host", "expected_sni"),
    [
        # IPv4, path + query preserved, default port
        (
            "https://hooks.example.com/sink?token=abc",
            "93.184.216.34",
            "https://93.184.216.34/sink?token=abc",
            "hooks.example.com",
            "hooks.example.com",
        ),
        # explicit non-default port preserved on both connect URL and Host header
        (
            "https://hooks.example.com:8443/retell",
            "93.184.216.34",
            "https://93.184.216.34:8443/retell",
            "hooks.example.com:8443",
            "hooks.example.com",
        ),
        # IPv6 is bracketed in the connect URL; Host/SNI stay the hostname
        (
            "https://hooks.example.com/",
            "2606:2800:220:1:248:1893:25c8:1946",
            "https://[2606:2800:220:1:248:1893:25c8:1946]/",
            "hooks.example.com",
            "hooks.example.com",
        ),
    ],
)
def test_pin_to_ip(
    url: str, ip: str, expected_url: str, expected_host: str, expected_sni: str
) -> None:
    connect_url, host_header, sni = ssrf_guard.pin_to_ip(url, ip)
    assert connect_url == expected_url
    assert host_header == expected_host
    assert sni == expected_sni
