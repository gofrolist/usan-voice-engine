"""Frozen conformance + behavior for the compat phone-number surface (Phase 2)."""

from __future__ import annotations

import uuid

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def _published_agent(compat_client, compat_headers) -> str:
    from tests.conftest import _create_and_publish_seed_agent

    return _create_and_publish_seed_agent(compat_client, compat_headers)


def test_import_get_delete_lifecycle(compat_client, compat_headers) -> None:
    agent_id = _published_agent(compat_client, compat_headers)
    r = compat_client.post(
        "/import-phone-number",
        json={
            "phone_number": "+15550000001",
            "termination_uri": "sip.example.com",
            "sip_trunk_auth_username": "u",
            "sip_trunk_auth_password": "s3cr3tPa$$w0rd",
            "outbound_agents": [{"agent_id": agent_id, "weight": 1}],
            "nickname": "main line",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phone_number"] == "+15550000001"
    assert body["phone_number_type"] == "custom"
    assert "auth_password" not in body.get("sip_outbound_trunk_config", {})
    assert "s3cr3tPa$$w0rd" not in r.text  # password never echoed
    assert_conforms(body, "PhoneNumberResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneNumberResponse")

    g = compat_client.get("/get-phone-number/+15550000001", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["outbound_agents"] == [{"agent_id": agent_id, "weight": 1.0}]

    d = compat_client.delete("/delete-phone-number/+15550000001", headers=compat_headers)
    assert d.status_code == 204
    assert d.content == b""
    assert (
        compat_client.get("/get-phone-number/+15550000001", headers=compat_headers).status_code
        == 404
    )


def test_duplicate_import_is_400(compat_client, compat_headers) -> None:
    payload = {"phone_number": "+15550000009", "termination_uri": "sip.example.com"}
    assert (
        compat_client.post("/import-phone-number", json=payload, headers=compat_headers).status_code
        == 201
    )
    dup = compat_client.post("/import-phone-number", json=payload, headers=compat_headers)
    assert dup.status_code == 400


def test_unknown_binding_agent_is_422(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/import-phone-number",
        json={
            "phone_number": "+15550000010",
            "termination_uri": "sip.example.com",
            "outbound_agents": [{"agent_id": "agent_" + uuid.uuid4().hex, "weight": 1}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
