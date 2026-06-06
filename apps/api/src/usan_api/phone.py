"""Phone-number normalization to E.164 for consistent elder lookup.

Inbound SIP caller-IDs arrive in varied formats. Telnyx most commonly delivers a
bare US national number (e.g. ``"6692388604"``) with no ``+`` and no country code,
because the trunk's origination number format is "E.164/National". Elders are
stored in strict E.164 (``"+16692388604"``), so an exact-match lookup on the raw
caller-ID misses and the agent falls back to a generic greeting.

This helper canonicalizes to E.164 before the lookup. It is US-defaulted by design
(the product serves US elders); already-E.164 inputs (incl. international) pass
through unchanged apart from separator stripping.
"""

import re

_NON_DIGITS = re.compile(r"\D")


def to_e164(raw: str | None) -> str | None:
    """Best-effort coerce a phone number to E.164, defaulting to US (+1).

    - ``None`` / blank -> ``None``.
    - Starts with ``+`` -> separators stripped, returned as E.164 (any country).
    - 10 digits -> ``+1`` prefixed (US national, the common Telnyx caller-ID).
    - 11 digits starting with ``1`` -> ``+`` prefixed (US with country code).
    - anything else -> ``+`` + digits (best effort; if it matches no elder, the
      caller is simply treated as unknown, which is safe).
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.startswith("+"):
        digits = _NON_DIGITS.sub("", stripped)
        return "+" + digits if digits else None
    digits = _NON_DIGITS.sub("", stripped)
    if not digits:
        return None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits
