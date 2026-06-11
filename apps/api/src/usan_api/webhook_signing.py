"""Outbound webhook signing: canonical bytes, HMAC-SHA256, secret generation (spec §7).

Wire format (Retell/Stripe-style)::

    X-Usan-Signature: v=<unix_ms>,d=<hex digest>

- ``v`` is the sender's Unix epoch MILLISECONDS at send time — fresh per
  attempt, so retries re-sign.
- ``d`` = ``hex(HMAC_SHA256(secret, f"{v}." + raw_body))`` over the exact
  bytes of the request body.
- Sign-what-you-send: JSONB round-trips do not preserve key order or byte
  form, so the body is serialized at send time via :func:`canonical_bytes`
  (stored payload + injected ``delivery_id``) and the signature is computed
  over those exact bytes. Receivers verify against the raw bytes received,
  never a re-serialized parse.
- Headers are routing convenience only and are NOT covered by the HMAC;
  receivers take ``event``/``delivery_id`` from the signed body.
- Secrets are 64 hex chars, server-generated, returned once at create,
  never logged, never re-readable via API (spec §8.3).
"""

import hashlib
import hmac
import json
import secrets
from typing import Any


def generate_secret() -> str:
    """Return a fresh per-endpoint signing secret: 64 hex chars (32 random bytes)."""
    return secrets.token_hex(32)


def canonical_bytes(body: dict[str, Any]) -> bytes:
    """``json.dumps(body, sort_keys=True, separators=(",", ":")).encode()``.

    Sign-what-you-send: JSONB does not preserve byte form (spec §7), so the
    canonical serialization happens at send time and these exact bytes are
    both signed and POSTed.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(secret: str, ts_ms: int, raw_body: bytes) -> str:
    """``hex(HMAC_SHA256(secret, f"{ts_ms}." + raw_body))``."""
    return hmac.new(secret.encode(), f"{ts_ms}.".encode() + raw_body, hashlib.sha256).hexdigest()


def signature_header(ts_ms: int, digest: str) -> str:
    """Render the ``X-Usan-Signature`` header value: ``v=<unix_ms>,d=<hex digest>``."""
    return f"v={ts_ms},d={digest}"
