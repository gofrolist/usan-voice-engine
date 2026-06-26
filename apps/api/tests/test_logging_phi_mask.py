"""The raw E.164 phone-number path segment is redacted from uvicorn.access log lines."""

from __future__ import annotations

from usan_api.logging_config import _mask_phi_path


def test_masks_phone_number_path_segment() -> None:
    line = '127.0.0.1:54321 - "GET /get-phone-number/+19495551234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "+19495551234" not in out
    assert "/get-phone-number/[redacted]" in out


def test_masks_update_and_delete_too() -> None:
    assert "+15550001111" not in _mask_phi_path("PATCH /update-phone-number/+15550001111 ...")
    assert "+15550002222" not in _mask_phi_path("DELETE /delete-phone-number/+15550002222 ...")


def test_leaves_other_paths_untouched() -> None:
    line = "GET /v2/list-phone-numbers?limit=50 HTTP/1.1"
    assert _mask_phi_path(line) == line
