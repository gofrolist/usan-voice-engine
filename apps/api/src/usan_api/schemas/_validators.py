"""Shared schema-validation constants.

Kept in one place so the E.164 contract is identical across every request
schema that accepts a phone number (elders, DNC).
"""

# E.164: leading '+', a non-zero country-code digit, then 7-14 more digits
# (8-15 digits total — the E.164 maximum).
E164_PATTERN = r"^\+[1-9]\d{7,14}$"

# Generous upper bound for the '+' plus up to 15 digits, with headroom.
PHONE_MAX_LENGTH = 20
