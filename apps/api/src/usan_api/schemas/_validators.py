"""Shared schema-validation constants.

Kept in one place so the E.164 contract is identical across every request
schema that accepts a phone number (contacts, DNC).
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# E.164: leading '+', a non-zero country-code digit, then 7-14 more digits
# (8-15 digits total — the E.164 maximum).
E164_PATTERN = r"^\+[1-9]\d{7,14}$"

# Generous upper bound for the '+' plus up to 15 digits, with headroom.
PHONE_MAX_LENGTH = 20

# Generous upper bound for an IANA zone name (longest real names are ~30 chars).
TIMEZONE_MAX_LENGTH = 64


def reject_nested_dynamic_vars(v: dict[str, object]) -> dict[str, object]:
    """Reject non-scalar dynamic_vars values (shared by call/schedule/batch schemas).

    dynamic_vars values are substituted into LLM prompts and SMS bodies as strings; a
    nested dict or list would be ``str()``-coerced into a Python repr — an
    operator-invisible, injection-adjacent surprise. Only JSON scalars
    (str/number/bool/null) are permitted; the byte cap is enforced separately.
    """
    for key, value in v.items():
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"dynamic_vars[{key!r}] must be a scalar (str/number/bool/null), "
                "not a nested object or array"
            )
    return v


def validate_iana_timezone(value: str) -> str:
    """Return ``value`` unchanged iff it names a resolvable IANA zone; else ValueError.

    Identical construction to ``quiet_hours._zone`` / ``schedule_windows._zone`` —
    so anything this accepts the runtime callers also accept (zero drift). A zone
    that won't construct must never reach the DB, where it would silently skip
    every call to that contact.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {value!r}") from exc
    return value
