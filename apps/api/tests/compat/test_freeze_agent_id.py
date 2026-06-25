"""Frozen: a created phone call always carries oracle-required agent_id/agent_version.

The 1a harness only exercised the override path (seeded_call passes override_agent_id).
These tests cover the BARE no-override path both ways: 422 when no published default
exists, and a conformant V2PhoneCallResponse when a published default DOES exist.
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import create_call

pytestmark = pytest.mark.frozen


def test_create_call_no_override_no_default_is_422(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    # Fresh org: no default outbound profile published.
    resp = create_call(compat_client, compat_headers)  # no override_agent_id
    assert resp.status_code == 422, resp.text
    # Never a 201 with a null-agent body.
    mock_dispatch.assert_not_awaited()


def test_create_call_no_override_with_published_default_conforms(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    resp = create_call(compat_client, compat_headers)  # no override; default resolves
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_id"], "agent_id must be present (V2CallBase required)"
    assert body["agent_version"] is not None, "agent_version must be present"
    assert_conforms(body, "V2PhoneCallResponse")
    # retell.types.CallResponse is typing.Union[WebCallResponse, PhoneCallResponse] — not a
    # Pydantic model. Use the concrete PhoneCallResponse arm directly.
    assert_sdk_roundtrip(body, "retell.types:PhoneCallResponse")
