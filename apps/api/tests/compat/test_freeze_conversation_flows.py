"""Frozen conformance for the compat conversation-flow surface (Phase 6a)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gpt-4.1"},
    "nodes": [],
    "global_prompt": "You are a helpful agent.",
}


def test_create_conforms(compat_client, compat_headers) -> None:
    r = compat_client.post("/create-conversation-flow", json=_FLOW, headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["conversation_flow_id"].startswith("conversation_flow_")
    assert isinstance(body["version"], int)
    assert isinstance(body["last_modification_timestamp"], int)
    assert_conforms(body, "ConversationFlowResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowResponse")


def test_get_update_list_conform(compat_client, compat_headers) -> None:
    cid = compat_client.post(
        "/create-conversation-flow", json=_FLOW, headers=compat_headers
    ).json()["conversation_flow_id"]

    g = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert_conforms(g.json(), "ConversationFlowResponse")

    u = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": "v2"}, headers=compat_headers
    )
    assert u.status_code == 200
    assert_conforms(u.json(), "ConversationFlowResponse")

    lst = compat_client.get("/v2/list-conversation-flows?limit=2", headers=compat_headers)
    assert lst.status_code == 200
    body = lst.json()
    assert isinstance(body["items"], list)
    for item in body["items"]:
        assert_conforms(item, "ConversationFlowResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowListResponse")
