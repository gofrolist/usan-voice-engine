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


def test_update_merge_and_traps(compat_client, compat_headers) -> None:
    compat_client.post(
        "/import-phone-number",
        json={
            "phone_number": "+15550000020",
            "termination_uri": "sip.example.com",
            "nickname": "x",
        },
        headers=compat_headers,
    )
    # update uses auth_* (NOT sip_trunk_auth_*); nickname nullable here; sms fields allowed.
    u = compat_client.patch(
        "/update-phone-number/+15550000020",
        json={
            "nickname": None,
            "auth_username": "u2",
            "auth_password": "p2",
            "inbound_sms_webhook_url": "https://sms.example.com:443/hook",
        },
        headers=compat_headers,
    )
    assert u.status_code == 200, u.text
    body = u.json()
    assert "nickname" not in body  # cleared -> omitted by exclude_none
    assert body["sip_outbound_trunk_config"]["auth_username"] == "u2"
    assert "auth_password" not in body["sip_outbound_trunk_config"]
    assert "p2" not in u.text
    assert_conforms(body, "PhoneNumberResponse")

    assert (
        compat_client.patch(
            "/update-phone-number/+19999999999", json={"nickname": "z"}, headers=compat_headers
        ).status_code
        == 404
    )


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    for n in range(3):
        compat_client.post(
            "/import-phone-number",
            json={"phone_number": f"+1555000003{n}", "termination_uri": "sip.example.com"},
            headers=compat_headers,
        )
    r = compat_client.get("/v2/list-phone-numbers?limit=2", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    for item in body["items"]:
        assert_conforms(item, "PhoneNumberResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneNumberListResponse")
