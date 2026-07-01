from __future__ import annotations

import uuid

_COMPONENT = {"name": "Collector", "nodes": []}


def _create(compat_client, compat_headers, **extra) -> dict:
    r = compat_client.post(
        "/create-conversation-flow-component",
        json={**_COMPONENT, **extra},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers, flex_mode=True)
    cid = body["conversation_flow_component_id"]
    assert cid.startswith("conversation_flow_component_")
    assert isinstance(body["user_modified_timestamp"], int)
    assert body["name"] == "Collector"
    assert body["flex_mode"] is True
    assert "version" not in body
    g = compat_client.get(f"/get-conversation-flow-component/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["conversation_flow_component_id"] == cid


def test_update_merges_top_level(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, flex_mode=True)["conversation_flow_component_id"]
    u1 = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": False},
        headers=compat_headers,
    )
    assert u1.status_code == 200, u1.text
    assert u1.json()["flex_mode"] is False
    # Omitting flex_mode preserves it; a new top-level field is added.
    u2 = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"tools": [{"type": "end_call"}]},
        headers=compat_headers,
    )
    assert u2.status_code == 200
    body = u2.json()
    assert body["flex_mode"] is False  # preserved
    assert body["tools"] == [{"type": "end_call"}]


def test_update_null_clears_field(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, flex_mode=True)["conversation_flow_component_id"]
    u = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": None},
        headers=compat_headers,
    )
    assert u.status_code == 200
    assert "flex_mode" not in u.json()  # explicit null cleared it -> omitted from echo
    g = compat_client.get(f"/get-conversation-flow-component/{cid}", headers=compat_headers).json()
    assert "flex_mode" not in g


def test_delete_then_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_component_id"]
    d = compat_client.delete(f"/delete-conversation-flow-component/{cid}", headers=compat_headers)
    assert d.status_code == 204
    assert d.content == b""
    r404 = compat_client.get(f"/get-conversation-flow-component/{cid}", headers=compat_headers)
    assert r404.status_code == 404


def test_update_after_delete_is_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_component_id"]
    d = compat_client.delete(f"/delete-conversation-flow-component/{cid}", headers=compat_headers)
    assert d.status_code == 204
    r = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": False},
        headers=compat_headers,
    )
    assert r.status_code == 404
    assert r.json() == {"status": 404, "message": "conversation flow component not found"}


def test_missing_required_field_is_422(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json={"name": "x"}, headers=compat_headers
    )
    assert r.status_code == 422


def test_malformed_id_is_422_and_missing_is_404(compat_client, compat_headers) -> None:
    r422 = compat_client.get(
        "/get-conversation-flow-component/not_a_component", headers=compat_headers
    )
    assert r422.status_code == 422
    missing = "conversation_flow_component_" + uuid.uuid4().hex
    r404 = compat_client.get(f"/get-conversation-flow-component/{missing}", headers=compat_headers)
    assert r404.status_code == 404


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    created = {
        _create(compat_client, compat_headers)["conversation_flow_component_id"] for _ in range(3)
    }
    r = compat_client.get("/v2/list-conversation-flow-components?limit=2", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    seen = {i["conversation_flow_component_id"] for i in body["items"]}
    key = body["pagination_key"]
    for _ in range(20):
        nxt = compat_client.get(
            f"/v2/list-conversation-flow-components?limit=2&pagination_key={key}",
            headers=compat_headers,
        ).json()
        seen |= {i["conversation_flow_component_id"] for i in nxt["items"]}
        if not nxt.get("has_more"):
            break
        key = nxt["pagination_key"]
    assert created <= seen


def test_server_keys_stripped_and_not_spoofable(
    compat_client, compat_headers, async_database_url
) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.compat import ids

    body = _create(
        compat_client,
        compat_headers,
        conversation_flow_component_id="conversation_flow_component_deadbeef",
        user_modified_timestamp=1,
    )
    # Server wins: the id is server-generated, not the client's spoof.
    assert body["conversation_flow_component_id"] != "conversation_flow_component_deadbeef"
    assert body["user_modified_timestamp"] != 1
    cid = ids.decode_conversation_flow_component_id(body["conversation_flow_component_id"])

    async def _read_config() -> dict:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(
                    text("SELECT config FROM conversation_flow_components WHERE id = :i"),
                    {"i": cid},
                )
        finally:
            await engine.dispose()

    stored = asyncio.run(_read_config())
    assert "conversation_flow_component_id" not in stored
    assert "user_modified_timestamp" not in stored
    assert stored["name"] == "Collector"  # real fields survive
