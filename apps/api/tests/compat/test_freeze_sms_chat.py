"""create-sms-chat conformance freeze (Phase 4b-1): SDK round-trip + get/list visibility."""

from __future__ import annotations

from .conformance import assert_conforms, assert_sdk_roundtrip

_SMS_FROM = "+15550000000"
_SMS_TO = "+15551234567"


def _create_sms(client, headers, agent_id):
    return client.post(
        "/create-sms-chat",
        json={
            "from_number": _SMS_FROM,
            "to_number": _SMS_TO,
            "override_agent_id": agent_id,
            "retell_llm_dynamic_variables": {"name": "Pat"},
        },
        headers=headers,
    )


def test_create_sms_chat_conforms(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(compat_client, compat_headers, web_agent_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "sms_chat"
    assert body["chat_id"].startswith("chat_")
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")


def test_sms_chat_visible_via_get_and_list(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    chat_id = _create_sms(compat_client, compat_headers, web_agent_id).json()["chat_id"]

    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers)
    assert got.status_code == 200, got.text
    assert got.json()["chat_type"] == "sms_chat"
    assert_sdk_roundtrip(got.json(), "retell.types:ChatResponse")

    listed = compat_client.post("/v3/list-chats", json={}, headers=compat_headers)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(i["chat_id"] == chat_id and i["chat_type"] == "sms_chat" for i in items)
    # V3 list items omit transcript / message_with_tool_calls
    item = next(i for i in items if i["chat_id"] == chat_id)
    assert "transcript" not in item
    assert "message_with_tool_calls" not in item
