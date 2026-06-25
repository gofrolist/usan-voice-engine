"""Contract-freeze tests for CreatePhoneCallRequest (T047 oracle freeze — Task 4).

Pinned against the RetellAI oracle: override_agent_version accepts an int version OR a
string tag ("latest", "prod").  Any change that breaks either accepted form is a
contract regression.
"""

from __future__ import annotations

import pytest

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
