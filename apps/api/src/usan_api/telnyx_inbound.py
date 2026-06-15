"""Inbound Telnyx webhook signature verification + family-task safety screen (US2).

Telnyx signs inbound webhooks with Ed25519 over ``f"{timestamp}|{raw_body}"``. The
signature arrives base64 in the ``telnyx-signature-ed25519`` header and the unix-second
timestamp in ``telnyx-timestamp``; the public key (base64, 32 raw bytes) is held in
``settings.telnyx_inbound_public_key`` (SecretStr, never logged).

Mirrors livekit_webhooks: the signature is verified FIRST (it binds the timestamp, so a
forged timestamp fails verification), THEN the replay age is checked. Both a forged
signature and a stale-but-valid delivery surface to the router as 401 so the response is
not an oracle. ``is_medically_unsafe`` is the deterministic FR-015 screen that flags a
task for operator review instead of relaying it verbatim.
"""

import base64
import binascii
import json
import re
import time
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from usan_api.settings import Settings


class InvalidTelnyxSignatureError(Exception):
    """The Telnyx webhook signature is missing, malformed, or does not verify."""


class TelnyxReplayError(Exception):
    """A signature-valid webhook whose timestamp is outside the replay window."""


def verify_telnyx_webhook(
    raw_body: bytes, signature_b64: str, timestamp: str, settings: Settings
) -> dict[str, Any]:
    """Verify a Telnyx webhook and return the parsed JSON payload.

    Raises InvalidTelnyxSignatureError on a missing/forged signature, TelnyxReplayError
    on a stale (replayed) delivery, and ValueError on a non-JSON body.
    """
    key = settings.telnyx_inbound_public_key
    if key is None:
        # Fail closed: an unconfigured key must never accept an unverified webhook.
        raise InvalidTelnyxSignatureError("TELNYX_INBOUND_PUBLIC_KEY not configured")
    if not signature_b64 or not timestamp:
        raise InvalidTelnyxSignatureError("missing signature or timestamp header")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key.get_secret_value()))
    except (binascii.Error, ValueError) as exc:
        raise InvalidTelnyxSignatureError("malformed TELNYX_INBOUND_PUBLIC_KEY") from exc

    signed_message = f"{timestamp}|".encode() + raw_body
    try:
        public_key.verify(base64.b64decode(signature_b64), signed_message)
    except (InvalidSignature, binascii.Error, ValueError) as exc:
        raise InvalidTelnyxSignatureError("signature verification failed") from exc

    # Signature is valid (and binds the timestamp) — now reject stale replays.
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise InvalidTelnyxSignatureError("invalid timestamp") from exc
    age_s = abs(time.time() - ts)
    if age_s > settings.webhook_max_age_s:
        raise TelnyxReplayError(f"webhook is {int(age_s)}s old (max {settings.webhook_max_age_s}s)")

    try:
        return cast(dict[str, Any], json.loads(raw_body))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON in webhook body") from exc


# High-precision phrases that mean "alter/withhold medication" — a family task like
# "tell mom to stop taking her heart pills" must be flagged for operator review (FR-015,
# spec edge case), never conveyed verbatim. Conservative on purpose: a missed flag is
# reviewed by the operator after delivery, but a false flag just adds a review step.
_MED_WORD = r"(?:pill|pills|med|meds|medication|medications|medicine|dose|doses|insulin)"
_UNSAFE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\bstop\s+(?:taking\s+)?(?:\w+\s+){{0,3}}{_MED_WORD}\b", re.IGNORECASE),
    re.compile(rf"\bskip\s+(?:\w+\s+){{0,3}}{_MED_WORD}\b", re.IGNORECASE),
    re.compile(rf"\bdon'?t\s+(?:take|give)\b(?:.{{0,30}}){_MED_WORD}\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:double|triple|extra|more|fewer|less)\b(?:.{{0,20}}){_MED_WORD}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"{_MED_WORD}\b(?:.{{0,20}})\b(?:stop|skip)\b", re.IGNORECASE),
)


def is_medically_unsafe(text: str) -> bool:
    """True if a family task conflicts with medical safety (FR-015) and needs review."""
    return any(p.search(text) for p in _UNSAFE_PATTERNS)


# Carrier-standard SMS opt-out keywords (CTIA). An inbound message that IS one of these
# means the sender wants no further texts/calls; the webhook route handles it BEFORE
# family-task intake (FR-038) so a STOP never lands as a relayed task.
_OPT_OUT_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
)
_NON_LETTER_RE = re.compile(r"[^a-z]")


def is_opt_out_keyword(text: str) -> bool:
    """True if the SMS body is exactly a standard opt-out keyword (FR-038).

    Normalizes by lower-casing and dropping every non-letter character, so "STOP.",
    "  stop  ", and "Unsubscribe" match while "stop by the store" does not — a stray
    keyword inside a longer sentence is NOT treated as an opt-out.
    """
    return _NON_LETTER_RE.sub("", text.lower()) in _OPT_OUT_KEYWORDS
