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


def test_update_after_delete_is_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_id"]
    d = compat_client.delete(f"/delete-conversation-flow/{cid}", headers=compat_headers)
    assert d.status_code == 204
    r = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": "x"}, headers=compat_headers
    )
    assert r.status_code == 404
    assert r.json() == {"status": 404, "message": "conversation flow not found"}


def test_update_missing_id_is_404(compat_client, compat_headers) -> None:
    import uuid

    missing = "conversation_flow_" + uuid.uuid4().hex
    r = compat_client.patch(
        f"/update-conversation-flow/{missing}", json={"global_prompt": "x"}, headers=compat_headers
    )
    assert r.status_code == 404


def test_server_keys_stripped_from_stored_config(
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
        version=999,
        conversation_flow_id="conversation_flow_deadbeef",
        last_modification_timestamp=1,
    )
    fid = ids.decode_conversation_flow_id(body["conversation_flow_id"])

    async def _read_config() -> dict:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(
                    text("SELECT config FROM conversation_flows WHERE id = :i"), {"i": fid}
                )
        finally:
            await engine.dispose()

    stored = asyncio.run(_read_config())
    assert "version" not in stored
    assert "conversation_flow_id" not in stored
    assert "last_modification_timestamp" not in stored
    assert stored["start_speaker"] == "agent"  # real fields survive


def test_update_null_clears_field(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, global_prompt="SECRET")["conversation_flow_id"]
    u = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": None}, headers=compat_headers
    )
    assert u.status_code == 200
    body = u.json()
    assert body["version"] == 1
    assert "global_prompt" not in body  # explicit null cleared it -> omitted from the echo
    # a subsequent GET also no longer carries it
    g = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers).json()
    assert "global_prompt" not in g


def test_client_cannot_spoof_server_fields(compat_client, compat_headers) -> None:
    body = _create(
        compat_client,
        compat_headers,
        version=999,
        conversation_flow_id="conversation_flow_deadbeef",
        last_modification_timestamp=1,
    )
    assert body["version"] == 0  # server wins, not the client's 999
    assert body["conversation_flow_id"].startswith("conversation_flow_")
    assert body["conversation_flow_id"] != "conversation_flow_deadbeef"
    cid = body["conversation_flow_id"]
    u = compat_client.patch(
        f"/update-conversation-flow/{cid}",
        json={"version": 999, "global_prompt": "x"},
        headers=compat_headers,
    )
    assert u.status_code == 200
    assert u.json()["version"] == 1  # server bump, not 999
    assert u.json()["conversation_flow_id"] == cid
