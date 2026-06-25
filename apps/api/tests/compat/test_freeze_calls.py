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
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, override_agent_version="latest")
    assert r.status_code == 201, r.text


def test_override_agent_version_accepts_int(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
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


# --- Task 7: Call sub-object shape freeze ------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "13 oracle violations in current serialization — all share the same root cause: "
        "null fields that the oracle does not mark nullable must be omitted (exclude_none). "
        "Distinct failing fields: collected_dynamic_variables, start_timestamp, end_timestamp, "
        "duration_ms, transcript, transcript_with_tool_calls (also wrong type: str vs array), "
        "recording_url, public_log_url, latency, disconnection_reason, call_analysis, "
        "call_cost, llm_token_usage. Green after Task 8 removes transcript_with_tool_calls "
        "and the serialization layer adopts exclude_none."
    ),
    strict=True,
)
def test_call_object_conforms_to_oracle(compat_client, compat_headers, seeded_call) -> None:
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert_conforms(body, "V2PhoneCallResponse")


def test_user_sentiment_default_is_null() -> None:
    from usan_api.compat.schemas.calls import CallAnalysis

    assert CallAnalysis().user_sentiment is None
