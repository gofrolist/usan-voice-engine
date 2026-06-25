"""Full-surface frozen conformance round-trip (Phase 1a final — Task 15).

Validates that the two cross-cutting wire shapes parse cleanly through the
pinned retell SDK models, giving us a Python-import-level SDK-compatibility
signal on top of the oracle-schema assertions in the per-endpoint freeze tests.

If either assertion fails with a pydantic.ValidationError the response body
has drifted from what the SDK expects — treat that as a CRITICAL finding and
do NOT weaken the assertion.
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def test_concurrency_sdk_roundtrip(compat_client, compat_headers) -> None:
    """GET /get-concurrency response must parse through the SDK ConcurrencyRetrieveResponse."""
    body = compat_client.get("/get-concurrency", headers=compat_headers).json()
    assert_sdk_roundtrip(body, "retell.types:ConcurrencyRetrieveResponse")


def test_call_object_sdk_roundtrip(compat_client, compat_headers, seeded_call) -> None:
    """GET /v2/get-call/{call_id} response must parse through the SDK PhoneCallResponse.

    retell.types.CallResponse is typing.Union[WebCallResponse, PhoneCallResponse] — not a
    Pydantic model, so model_validate is only available on the concrete arms.  The seeded
    call is always a phone call (call_type='phone_call'), so we validate against the
    concrete PhoneCallResponse arm of the union.
    """
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert_sdk_roundtrip(body, "retell.types:PhoneCallResponse")
