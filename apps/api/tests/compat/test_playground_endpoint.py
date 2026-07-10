"""Endpoint + oracle-conformance tests for POST /agent-playground-completion/{agent_id}
(Phase 7 slice 1).

Deviates from the original brief's raw-SQL seed helper: `web_agent_id` (defined in
tests/compat/conftest.py) already publishes a real agent through the compat HTTP
surface (create-retell-llm -> create-agent -> publish-agent-version), which yields a
profile with a genuinely AgentConfig-valid draft_config — no hand-written INSERT / column
guessing needed, and it composes cleanly with compat_client's per-request session.
"""

from __future__ import annotations

import pytest

from usan_api.vertex_test import VertexTurn

pytestmark = pytest.mark.usefixtures("gcp_project_set")


def test_endpoint_200_happy_path(compat_client, compat_headers, web_agent_id, monkeypatch) -> None:
    async def fake_turn(**kwargs):
        return VertexTurn(text="Hi from playground")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    r = compat_client.post(
        f"/agent-playground-completion/{web_agent_id}",
        headers=compat_headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"][0]["role"] == "agent"
    assert body["messages"][0]["content"] == "Hi from playground"
    assert "call_ended" not in body


@pytest.mark.frozen
def test_endpoint_response_conforms(
    compat_client, compat_headers, web_agent_id, monkeypatch
) -> None:
    from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

    async def fake_turn(**kwargs):
        return VertexTurn(text="conformant text")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    r = compat_client.post(
        f"/agent-playground-completion/{web_agent_id}",
        headers=compat_headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert_sdk_roundtrip(payload, "retell.types:PlaygroundCompletionResponse")
    for msg in payload["messages"]:
        assert_conforms(msg, "MessageOrToolCall")


def test_endpoint_422_empty_messages(compat_client, compat_headers, web_agent_id) -> None:
    r = compat_client.post(
        f"/agent-playground-completion/{web_agent_id}",
        headers=compat_headers,
        json={"messages": []},
    )
    assert r.status_code == 422, r.text
