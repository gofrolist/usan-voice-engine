r"""Retell-compatible webhook signing (feature 003 / US2, T027).

Mirror-image of the real retell-sdk ``verify()`` (transcribed from
github.com/RetellAI/retell-python-sdk src/retell/lib/webhook_auth.py and the npm
retell-sdk webhook_auth.ts), so a signature WE emit is accepted by the customer CRM's
unmodified ``Retell.verify(raw_body, secret, signature)``:

    digest    = hex( HMAC_SHA256(key=secret, msg = raw_body + str(ts_ms)) )   # lowercase
    header    = "v={ts_ms},d={digest}"            # parsed by regex v=(\d+),d=(.*)

Two byte-exact rules the SDK enforces:
- The HMAC message is ``raw_body`` concatenated DIRECTLY with the decimal-ms timestamp —
  NO separator, NO prefix (unlike the native X-Usan ``f"{ts}." + body`` scheme).
- ``ts_ms`` MUST be the true current wall-clock ms at send time: verify() rejects (before
  the HMAC) when ``abs(receiver_now_ms - ts_ms) > 300000`` (a ±5-minute freshness window).
  So callers pass a fresh ``int(time.time() * 1000)`` per attempt — a replayed/stale ts
  fails even with a correct digest.

Unlike the native signer, the HMAC key is a DEDICATED per-subscription secret
(``CompatWebhookEndpoint.secret``), NOT the bearer API key — the CRM configures that secret
as its verify() key. Sign-what-you-send: the digest is over the EXACT bytes POSTed.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, raw_body: bytes, ts_ms: int) -> str:
    """``hex(HMAC_SHA256(secret, raw_body + str(ts_ms)))`` (lowercase) — the ``d=`` digest."""
    return hmac.new(secret.encode(), raw_body + str(ts_ms).encode(), hashlib.sha256).hexdigest()


def signature_header(ts_ms: int, digest: str) -> str:
    """Render the ``x-retell-signature`` value: ``v=<ts_ms>,d=<hex digest>``."""
    return f"v={ts_ms},d={digest}"
