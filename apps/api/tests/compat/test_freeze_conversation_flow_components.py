"""Frozen conformance for the compat conversation-flow-component surface (Phase 6b)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen

_COMPONENT = {
    "name": "Customer Information Collector",
    "nodes": [],
    "flex_mode": False,
}


def test_create_conforms(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json=_COMPONENT, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["conversation_flow_component_id"].startswith("conversation_flow_component_")
    assert isinstance(body["user_modified_timestamp"], int)
    assert_conforms(body, "ConversationFlowComponentResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowComponentResponse")


def test_get_update_list_conform(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json=_COMPONENT, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    cid = r.json()["conversation_flow_component_id"]

    g = compat_client.get(f"/get-conversation-flow-component/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert_conforms(g.json(), "ConversationFlowComponentResponse")

    u = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": True},
        headers=compat_headers,
    )
    assert u.status_code == 200
    assert_conforms(u.json(), "ConversationFlowComponentResponse")

    lst = compat_client.get("/v2/list-conversation-flow-components?limit=2", headers=compat_headers)
    assert lst.status_code == 200
    body = lst.json()
    assert isinstance(body["items"], list)
    for item in body["items"]:
        assert_conforms(item, "ConversationFlowComponentResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowComponentListResponse")
