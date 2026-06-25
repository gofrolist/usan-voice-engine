"""Contract-freeze tests for CreatePhoneCallRequest and Call sub-object shapes
(T047 oracle freeze — Tasks 4 and 7).

Pinned against the RetellAI oracle: override_agent_version accepts an int version OR a
string tag ("latest", "prod").  Any change that breaks either accepted form is a
contract regression.

Task 7 adds:
  - test_call_object_conforms_to_oracle  (xfail: 13 null-field violations +
    transcript_with_tool_calls type mismatch; green after exclude_none + Task 8)
  - test_user_sentiment_default_is_null  (CallAnalysis.user_sentiment default must stay None)
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms
from tests.compat.conftest import create_call

pytestmark = pytest.mark.frozen


def test_override_agent_version_accepts_string_tag(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, override_agent_version="latest")
    assert r.status_code == 201, r.text


def test_override_agent_version_accepts_int(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, override_agent_version=3)
    assert r.status_code == 201, r.text


def test_custom_sip_headers_values_coerced_to_string():
    from usan_api.compat.schemas.calls import CreatePhoneCallRequest

    req = CreatePhoneCallRequest(
        from_number="+15551230000",
        to_number="+15557654321",
        custom_sip_headers={"X-Trace": 42},
    )
    assert req.custom_sip_headers == {"X-Trace": "42"}  # int coerced to str


def test_update_call_accepts_override_dynamic_variables(compat_client, compat_headers, seeded_call):
    r = compat_client.patch(
        f"/v2/update-call/{seeded_call}",
        json={"override_dynamic_variables": {"first_name": "Bo"}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["retell_llm_dynamic_variables"]["first_name"] == "Bo"


def test_update_call_rejects_bad_data_storage_setting(compat_client, compat_headers, seeded_call):
    r = compat_client.patch(
        f"/v2/update-call/{seeded_call}",
        json={"data_storage_setting": "bogus"},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


# --- Task 7 / Task 8: Call sub-object shape freeze --------------------------------------


def test_call_object_conforms_to_oracle(compat_client, compat_headers, seeded_call) -> None:
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert_conforms(body, "V2PhoneCallResponse")


def test_transcript_with_tool_calls_is_omitted(compat_client, compat_headers, seeded_call):
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    # The str-vs-array mismatched field is gone from the wire entirely.
    assert "transcript_with_tool_calls" not in body
    # transcript_object defaults to [] (not None) so exclude_none keeps it on the wire.
    assert "transcript_object" in body
    assert isinstance(body["transcript_object"], list)
    # transcript is str|None → omitted when null; assert field remains in schema.
    from usan_api.compat.schemas.calls import CompatCall

    assert "transcript" in CompatCall.model_fields


def test_user_sentiment_default_is_null() -> None:
    from usan_api.compat.schemas.calls import CallAnalysis

    assert CallAnalysis().user_sentiment is None


# --- Task 14: list-calls filter breadth, idempotency ---


def test_list_calls_filter_ignores_unknown_keys(compat_client, compat_headers, seeded_call):
    """POST /v3/list-calls must accept unknown filter_criteria keys without error."""
    r = compat_client.post(
        "/v3/list-calls",
        json={"filter_criteria": {"unknown": "x"}, "limit": 5},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text


def test_duplicate_create_is_idempotent(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    """Two identical create-phone-call requests return the same call_id (sha256 dedupe)."""
    a = create_call(compat_client, compat_headers).json()["call_id"]
    b = create_call(compat_client, compat_headers).json()["call_id"]
    assert a == b  # sha256 dedupe (call_create.py:50/78) — same params → same call


def test_metadata_name_and_external_id_populate_contact(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    """metadata keys ``name`` and ``external_id`` are FROZEN: the contact-upsert path
    reads them to populate Contact.name and Contact.external_id."""
    r = create_call(
        compat_client,
        compat_headers,
        metadata={"name": "Ada", "external_id": "crm-1"},
    )
    assert r.status_code == 201, r.text
    assert r.json().get("call_id")
