"""Frozen: GET /v2/list-export-requests is a conformant empty-list stub (Phase 2)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def test_list_export_requests_is_empty(compat_client, compat_headers) -> None:
    r = compat_client.get(
        "/v2/list-export-requests?limit=50&sort_order=descending&pagination_key=anything",
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "has_more": False}
    assert "pagination_key" not in body
    # SDK round-trip (ExportRequestListResponse is an SDK type; there is no oracle component).
    assert_sdk_roundtrip(body, "retell.types:ExportRequestListResponse")
