from __future__ import annotations

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gpt-4.1"},
    "nodes": [],
}


def _create(compat_client, compat_headers, **extra) -> dict:
    r = compat_client.post(
        "/create-conversation-flow", json={**_FLOW, **extra}, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers, global_prompt="hi")
    cid = body["conversation_flow_id"]
    assert cid.startswith("conversation_flow_")
    assert body["version"] == 0
    assert body["start_speaker"] == "agent"
    assert body["global_prompt"] == "hi"
    g = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["conversation_flow_id"] == cid


def test_update_merges_top_level_and_bumps_version(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, global_prompt="a")["conversation_flow_id"]
    u1 = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": "b"}, headers=compat_headers
    )
    assert u1.status_code == 200, u1.text
    assert u1.json()["version"] == 1
    assert u1.json()["global_prompt"] == "b"
    # Omitting global_prompt preserves it; a new top-level field is added; version bumps again.
    u2 = compat_client.patch(
        f"/update-conversation-flow/{cid}",
        json={"model_temperature": 0.5},
        headers=compat_headers,
    )
    assert u2.status_code == 200
    body = u2.json()
    assert body["version"] == 2
    assert body["global_prompt"] == "b"  # preserved
    assert body["model_temperature"] == 0.5


def test_version_query_param_is_accepted_and_ignored(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_id"]
    g = compat_client.get(f"/get-conversation-flow/{cid}?version=7", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["version"] == 0  # current, not 7


def test_delete_then_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_id"]
    d = compat_client.delete(f"/delete-conversation-flow/{cid}", headers=compat_headers)
    assert d.status_code == 204
    assert d.content == b""
    r404 = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers)
    assert r404.status_code == 404


def test_malformed_id_is_422_and_missing_is_404(compat_client, compat_headers) -> None:
    import uuid

    r422 = compat_client.get("/get-conversation-flow/not_a_flow_id", headers=compat_headers)
    assert r422.status_code == 422
    missing = "conversation_flow_" + uuid.uuid4().hex
    r404 = compat_client.get(f"/get-conversation-flow/{missing}", headers=compat_headers)
    assert r404.status_code == 404


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    created = {_create(compat_client, compat_headers)["conversation_flow_id"] for _ in range(3)}
    r = compat_client.get("/v2/list-conversation-flows?limit=2", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    # Walk pages until the 3 we created are all seen (siblings from -n auto are tolerated).
    seen = {i["conversation_flow_id"] for i in body["items"]}
    key = body["pagination_key"]
    for _ in range(20):
        nxt = compat_client.get(
            f"/v2/list-conversation-flows?limit=2&pagination_key={key}", headers=compat_headers
        ).json()
        seen |= {i["conversation_flow_id"] for i in nxt["items"]}
        if not nxt.get("has_more"):
            break
        key = nxt["pagination_key"]
    assert created <= seen
