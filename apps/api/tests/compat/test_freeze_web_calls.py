"""Contract freeze for POST /v2/create-web-call (RetellAI parity Phase 3)."""

from __future__ import annotations

from .conformance import assert_conforms, assert_sdk_roundtrip


def _create_web_call(client, headers, agent_id, **overrides):
    body = {"agent_id": agent_id}
    body.update(overrides)
    return client.post("/v2/create-web-call", json=body, headers=headers)


def test_create_web_call_requires_key(compat_client, web_agent_id, mock_web_dispatch):
    r = compat_client.post("/v2/create-web-call", json={"agent_id": web_agent_id})
    assert r.status_code == 401


def test_create_web_call_conforms(compat_client, compat_headers, web_agent_id, mock_web_dispatch):
    r = _create_web_call(
        compat_client,
        compat_headers,
        web_agent_id,
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["call_type"] == "web_call"
    assert body["access_token"]
    assert body["call_status"] == "registered"
    for phone_field in ("from_number", "to_number", "direction", "telephony_identifier"):
        assert phone_field not in body
    assert_conforms(body, "V2WebCallResponse")
    assert_sdk_roundtrip(body, "retell.types:WebCallResponse")


def test_create_web_call_rejects_malformed_agent_id(
    compat_client, compat_headers, mock_web_dispatch
):
    r = _create_web_call(compat_client, compat_headers, "not-an-agent-id")
    assert r.status_code == 422


def test_create_web_call_rejects_unpublished_agent(
    compat_client, compat_headers, mock_web_dispatch
):
    r = _create_web_call(compat_client, compat_headers, "agent_" + "0" * 32)
    assert r.status_code == 422


def test_heavier_optional_fields_accepted(
    compat_client, compat_headers, web_agent_id, mock_web_dispatch
):
    r = _create_web_call(
        compat_client,
        compat_headers,
        web_agent_id,
        agent_override={"voice_id": "v"},
        current_node_id="n1",
        current_state="s1",
    )
    assert r.status_code == 201, r.text


def test_metadata_round_trips_on_get(
    compat_client, compat_headers, web_agent_id, mock_web_dispatch
):
    created = _create_web_call(
        compat_client,
        compat_headers,
        web_agent_id,
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
        agent_override={"voice_id": "v"},
    ).json()
    got = compat_client.get(f"/v2/get-call/{created['call_id']}", headers=compat_headers).json()
    assert got["call_type"] == "web_call"
    assert got["metadata"] == {"external_id": "e1"}
    assert got["retell_llm_dynamic_variables"] == {"name": "Pat"}
    # the accepted-but-not-honored fields never leak into the echo
    assert "agent_override" not in got["metadata"]
    assert "__meta_unhonored__" not in got["retell_llm_dynamic_variables"]
    assert_conforms(got, "V2WebCallResponse")
    assert_sdk_roundtrip(got, "retell.types:WebCallResponse")
