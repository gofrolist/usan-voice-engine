"""create-sms-chat conformance freeze (Phase 4b-1 + 4b-2): SDK round-trip + get/list visibility."""

from __future__ import annotations

from sqlalchemy import text as _sa_text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

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


def _append_two_way(dsn: str, chat_id_uuid: str) -> None:
    """Insert an inbound role='sms' turn + an agent reply for an existing session, so get-chat
    serializes a two-way sms_chat (the 4b-2 reply-engine end state) for conformance checks."""
    import asyncio

    async def _run() -> None:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                nxt = (
                    await conn.execute(
                        _sa_text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages "
                            "WHERE chat_session_id = :sid"
                        ),
                        {"sid": chat_id_uuid},
                    )
                ).scalar_one()
                await conn.execute(
                    _sa_text(
                        "INSERT INTO chat_messages"
                        " (chat_session_id, seq, role, content, provider_message_id)"
                        " VALUES (:sid, :seq, 'sms', 'hi back', 'tx-in-1')"
                    ),
                    {"sid": chat_id_uuid, "seq": nxt},
                )
                await conn.execute(
                    _sa_text(
                        "INSERT INTO chat_messages (chat_session_id, seq, role, content) "
                        "VALUES (:sid, :seq, 'agent', 'Glad to hear it!')"
                    ),
                    {"sid": chat_id_uuid, "seq": nxt + 1},
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_two_way_sms_chat_conforms_with_sms_role(
    compat_client,
    compat_headers,
    web_agent_id,
    sms_messaging_enabled,
    mock_send_sms,
    async_database_url,
):
    from usan_api.compat import ids

    chat_id = _create_sms(compat_client, compat_headers, web_agent_id).json()["chat_id"]
    _append_two_way(async_database_url, str(ids.decode_chat_id(chat_id)))

    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers).json()
    roles = [m["role"] for m in got["message_with_tool_calls"]]
    assert "sms" in roles  # oracle-faithful inbound role
    assert "agent" in roles
    assert_conforms(got, "ChatResponse")
    assert_sdk_roundtrip(got, "retell.types:ChatResponse")
