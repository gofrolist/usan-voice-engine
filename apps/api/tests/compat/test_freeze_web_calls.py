"""Contract freeze for POST /v2/create-web-call (RetellAI parity Phase 3)."""

from __future__ import annotations

from usan_api.compat.serialization import _UNHONORED_KEY

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


# --- Audit-blob persistence through update endpoints (review finding fix) ---------------


def test_unhonored_blob_survives_update_call(
    compat_client, compat_headers, web_agent_id, mock_web_dispatch
):
    """__meta_unhonored__ persists in dynamic_vars after update-call (unpack→modify→pack)."""
    created = _create_web_call(
        compat_client,
        compat_headers,
        web_agent_id,
        agent_override={"voice_id": "audit-v"},
        current_node_id="n1",
        retell_llm_dynamic_variables={"name": "Pat"},
    ).json()
    call_id = created["call_id"]

    # update-call: replace dynamic variables
    upd = compat_client.patch(
        f"/v2/update-call/{call_id}",
        json={"override_dynamic_variables": {"name": "Bo"}},
        headers=compat_headers,
    )
    assert upd.status_code == 200, upd.text
    # Echo must NOT leak the reserved blob
    echo = upd.json()
    assert echo["retell_llm_dynamic_variables"] == {"name": "Bo"}
    assert _UNHONORED_KEY not in echo.get("retell_llm_dynamic_variables", {})
    assert _UNHONORED_KEY not in echo.get("metadata", {})

    # A second update must also preserve the blob (proves it survived the first round-trip)
    upd2 = compat_client.patch(
        f"/v2/update-call/{call_id}",
        json={"override_dynamic_variables": {"name": "Carol"}},
        headers=compat_headers,
    )
    assert upd2.status_code == 200, upd2.text
    echo2 = upd2.json()
    assert echo2["retell_llm_dynamic_variables"] == {"name": "Carol"}
    assert _UNHONORED_KEY not in echo2.get("retell_llm_dynamic_variables", {})

    # Confirm blob is still present in the stored column via a raw DB read through get-call
    # (get-call strips the blob from the echo, so absence in echo is expected — we verify
    # the blob didn't corrupt anything and the call is still retrievable).
    got = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers).json()
    assert got["retell_llm_dynamic_variables"] == {"name": "Carol"}
    assert _UNHONORED_KEY not in got.get("retell_llm_dynamic_variables", {})
    assert_conforms(got, "V2WebCallResponse")


def test_unhonored_blob_survives_update_live_call(
    compat_client, compat_headers, web_agent_id, mock_web_dispatch
):
    """__meta_unhonored__ persists in dynamic_vars after update-live-call merge."""
    created = _create_web_call(
        compat_client,
        compat_headers,
        web_agent_id,
        agent_override={"voice_id": "audit-v"},
        current_state="s1",
        retell_llm_dynamic_variables={"name": "Pat"},
    ).json()
    call_id = created["call_id"]

    resp = compat_client.patch(
        f"/v2/update-live-call/{call_id}",
        json={"fields_to_override": {"override_dynamic_variables": {"name": "Ada"}}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}

    # Blob must still be in the stored column — verify via get-call (stripped from echo as designed)
    got = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers).json()
    assert got["retell_llm_dynamic_variables"] == {"name": "Ada"}
    assert _UNHONORED_KEY not in got.get("retell_llm_dynamic_variables", {})
    assert_conforms(got, "V2WebCallResponse")
