"""PHI masking in uvicorn access-log lines.

uvicorn logs paths via urllib.parse.quote(scope['path']), so a literal '+'
becomes '%2B'. The mask must fire on all encoded/punctuated forms, not just
the bare E.164 form the old regex handled.
"""

from __future__ import annotations

from usan_api.logging_config import _mask_phi_path

# ---------------------------------------------------------------------------
# GET /get-phone-number/ — uvicorn-encoded form (the real production line)
# ---------------------------------------------------------------------------


def test_masks_percent_encoded_e164_in_access_log() -> None:
    """The %2B-encoded form emitted by uvicorn is masked."""
    line = '127.0.0.1:54321 - "GET /get-phone-number/%2B19495551234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "/get-phone-number/[redacted]" in out


def test_no_encoded_digits_leak_after_mask() -> None:
    """%2B19495551234 must not appear — neither the token nor the bare digits."""
    line = '127.0.0.1:54321 - "GET /get-phone-number/%2B19495551234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "%2B19495551234" not in out
    assert "19495551234" not in out


# ---------------------------------------------------------------------------
# Punctuated / hyphenated form (e.g. +1-949-555-1234)
# ---------------------------------------------------------------------------


def test_masks_percent_encoded_punctuated_number() -> None:
    """%2B1-949-555-1234 form is fully masked with no digit substring leak."""
    line = '127.0.0.1:54321 - "GET /get-phone-number/%2B1-949-555-1234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "/get-phone-number/[redacted]" in out
    assert "%2B1-949-555-1234" not in out
    # Individual digit groups must not appear
    assert "19495551234" not in out


# ---------------------------------------------------------------------------
# Defense-in-depth: literal '+' form (pre-encoding, e.g. test helpers)
# ---------------------------------------------------------------------------


def test_masks_literal_plus_e164_defense_in_depth() -> None:
    """The literal '+' form (non-uvicorn callers / test helpers) is also masked."""
    line = '127.0.0.1:54321 - "GET /get-phone-number/+19495551234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "/get-phone-number/[redacted]" in out
    assert "+19495551234" not in out


# ---------------------------------------------------------------------------
# PATCH /update-phone-number/ and DELETE /delete-phone-number/
# ---------------------------------------------------------------------------


def test_masks_update_phone_number_path() -> None:
    """PATCH /update-phone-number/ percent-encoded form is masked."""
    line = "PATCH /update-phone-number/%2B15550001111 HTTP/1.1"
    out = _mask_phi_path(line)
    assert "/update-phone-number/[redacted]" in out
    assert "15550001111" not in out


def test_masks_delete_phone_number_path() -> None:
    """DELETE /delete-phone-number/ percent-encoded form is masked."""
    line = "DELETE /delete-phone-number/%2B15550002222 HTTP/1.1"
    out = _mask_phi_path(line)
    assert "/delete-phone-number/[redacted]" in out
    assert "15550002222" not in out


# ---------------------------------------------------------------------------
# Non-phone paths must be returned byte-identical
# ---------------------------------------------------------------------------


def test_list_phone_numbers_path_untouched() -> None:
    """The list endpoint path (no phone number segment) is not modified."""
    line = '"GET /v2/list-phone-numbers?limit=50 HTTP/1.1"'
    assert _mask_phi_path(line) == line
