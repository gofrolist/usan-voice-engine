"""T025 — compat webhook signature contract test.

A FAITHFUL replica of the real retell-sdk symmetric ``verify()`` (transcribed from
RetellAI/retell-python-sdk src/retell/lib/webhook_auth.py) is the oracle: our signer must
produce a header the CRM's unmodified ``Retell.verify(raw_body, secret, signature)`` accepts,
and the oracle must reject a tampered body, the wrong key, a stale/future timestamp, and a
malformed header. ``secret`` is the dedicated per-subscription signing secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

from usan_api.compat import webhook_signature

_FIVE_MIN_MS = 5 * 60 * 1000


def retell_verify(
    raw_body: bytes, secret: str, signature: str, *, now_ms: int | None = None
) -> bool:
    """Byte-for-byte replica of retell-sdk symmetric verify(): regex-parse ``v=,d=``, reject
    if ``abs(now - poststamp) > 5min`` BEFORE the HMAC, then ``HMAC_SHA256(secret, body +
    str(poststamp))`` lowercase-hex compared for equality."""
    match = re.search(r"v=(\d+),d=(.*)", signature)
    if not match:
        return False
    poststamp = int(match.group(1))
    post_digest = match.group(2)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now - poststamp) > _FIVE_MIN_MS:
        return False
    expected = hmac.new(
        secret.encode(), raw_body + str(poststamp).encode(), hashlib.sha256
    ).hexdigest()
    return expected == post_digest


_SECRET = "a" * 64
_BODY = b'{"event":"call_started","call":{"call_id":"deadbeef"}}'


def _header(secret: str, body: bytes, ts_ms: int) -> str:
    return webhook_signature.signature_header(ts_ms, webhook_signature.sign(secret, body, ts_ms))


def test_signer_accepted_by_retell_replica() -> None:
    ts = int(time.time() * 1000)
    assert retell_verify(_BODY, _SECRET, _header(_SECRET, _BODY, ts)) is True


def test_tampered_body_rejected() -> None:
    ts = int(time.time() * 1000)
    sig = _header(_SECRET, _BODY, ts)
    assert retell_verify(_BODY + b" ", _SECRET, sig) is False


def test_wrong_key_rejected() -> None:
    ts = int(time.time() * 1000)
    sig = _header(_SECRET, _BODY, ts)
    assert retell_verify(_BODY, "b" * 64, sig) is False


def test_stale_timestamp_rejected() -> None:
    now = int(time.time() * 1000)
    stale = now - (_FIVE_MIN_MS + 1000)  # > 5 min in the past
    # A correct HMAC over the stale ts still fails the freshness window.
    assert retell_verify(_BODY, _SECRET, _header(_SECRET, _BODY, stale), now_ms=now) is False


def test_future_timestamp_rejected() -> None:
    now = int(time.time() * 1000)
    future = now + (_FIVE_MIN_MS + 1000)  # > 5 min skew (abs window rejects both directions)
    assert retell_verify(_BODY, _SECRET, _header(_SECRET, _BODY, future), now_ms=now) is False


def test_malformed_header_rejected() -> None:
    assert retell_verify(_BODY, _SECRET, "not-a-signature") is False


def test_no_separator_in_signed_message() -> None:
    # Guard the byte-exact rule: the HMAC message is body + str(ts) with NO separator. A
    # signer that inserted a '.' (the native X-Usan scheme) would NOT match this digest.
    ts = 1_700_000_000_000
    digest = webhook_signature.sign(_SECRET, _BODY, ts)
    expected = hmac.new(_SECRET.encode(), _BODY + b"1700000000000", hashlib.sha256).hexdigest()
    assert digest == expected
