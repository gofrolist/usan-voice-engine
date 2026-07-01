"""End-to-end: a flow-bound chat agent executes its DAG when the flag is on (6-runtime-chat)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.compat.conftest import RETELL_VOICE
from usan_api.vertex_test import VertexTurn

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gemini-2.5-flash"},
    "global_prompt": "You are Flo.",
    "start_node_id": "n1",
    "nodes": [
        {
            "id": "n1",
            "type": "conversation",
            "instruction": {"type": "prompt", "text": "NODE_ONE greet the caller."},
            "edges": [
                {
                    "id": "e1",
                    "transition_condition": {"type": "prompt", "prompt": "Always"},
                    "destination_node_id": "n2",
                }
            ],
        },
        {
            "id": "n2",
            "type": "end",
            "instruction": {"type": "prompt", "text": "NODE_TWO say goodbye."},
        },
    ],
}

_FUNCTION_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gemini-2.5-flash"},
    "start_node_id": "f1",
    "nodes": [
        {
            "id": "f1",
            "type": "function",
            "tool_id": "t",
            "tool_type": "local",
            "wait_for_result": True,
        }
    ],
}


def _create_flow(compat_client, compat_headers, body) -> str:
    r = compat_client.post("/create-conversation-flow", json=body, headers=compat_headers)
    assert r.status_code == 201, r.text
    return r.json()["conversation_flow_id"]


def _create_flow_agent(compat_client, compat_headers, flow_id) -> str:
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_id},
            "voice_id": RETELL_VOICE,
            "agent_name": "Flow Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["agent_id"]


def _start_chat(compat_client, compat_headers, agent_id) -> str:
    r = compat_client.post("/create-chat", json={"agent_id": agent_id}, headers=compat_headers)
    assert r.status_code == 201, r.text
    return r.json()["chat_id"]


def _last_content(resp_json: dict[str, Any]) -> str:
    # create-chat-completion returns CompatChatCompletion{messages: [CompatChatMessage, ...]}
    # (see routers/chats.py create_chat_completion) — the new agent message(s).
    return resp_json["messages"][-1]["content"]


@pytest.fixture
def spy_vertex(monkeypatch):
    """Return a canned reply that echoes the system instruction's node marker so the test can
    assert which node spoke. Patch BOTH the runtime and the chat_service default path."""

    async def _fake(**kw: Any) -> VertexTurn:
        sysi = kw.get("system_instruction", "")
        marker = "NODE_TWO" if "NODE_TWO" in sysi else "NODE_ONE" if "NODE_ONE" in sysi else "REPLY"
        return VertexTurn(text=f"reply-from-{marker}")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _fake)
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _fake)


def test_flag_on_flow_agent_executes_first_node(
    compat_client, compat_headers, flow_runtime_on, spy_vertex
):
    flow_id = _create_flow(compat_client, compat_headers, _FLOW)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id)
    chat_id = _start_chat(compat_client, compat_headers, agent_id)
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hello"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    # First turn ENTERS at n1 (no transition eval) and speaks NODE_ONE.
    assert _last_content(r.json()) == "reply-from-NODE_ONE"


def test_flag_on_second_turn_advances_via_always_edge(
    compat_client, compat_headers, flow_runtime_on, spy_vertex
):
    flow_id = _create_flow(compat_client, compat_headers, _FLOW)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id)
    chat_id = _start_chat(compat_client, compat_headers, agent_id)
    compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hello"},
        headers=compat_headers,
    )
    r2 = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "ok bye"},
        headers=compat_headers,
    )
    assert r2.status_code == 201, r2.text
    # cursor was at n1; the Always edge advances to n2 (end) which speaks NODE_TWO.
    assert _last_content(r2.json()) == "reply-from-NODE_TWO"


def test_non_runnable_flow_falls_back_to_single_prompt(
    compat_client, compat_headers, flow_runtime_on, spy_vertex
):
    flow_id = _create_flow(compat_client, compat_headers, _FUNCTION_FLOW)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id)
    chat_id = _start_chat(compat_client, compat_headers, agent_id)
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hello"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    # function-node flow is NOT runnable -> single-prompt path -> REPLY marker (no NODE_*).
    assert _last_content(r.json()) == "reply-from-REPLY"


def test_flag_off_ignores_flow_binding(compat_client, compat_headers, gcp_project_set, spy_vertex):
    # gcp_project_set (not flow_runtime_on) => flag stays default off.
    flow_id = _create_flow(compat_client, compat_headers, _FLOW)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id)
    chat_id = _start_chat(compat_client, compat_headers, agent_id)
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hello"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    # flag off => single-prompt path => REPLY, never a NODE_* marker.
    assert _last_content(r.json()) == "reply-from-REPLY"


# --- cursor-identity: repointing the agent to a different flow must not resume at a same-id node


def _convo(node_id: str, marker: str, edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "conversation",
        "instruction": {"type": "prompt", "text": f"{marker} instruction"},
        "edges": edges or [],
    }


def _always(dest: str) -> dict[str, Any]:
    return {
        "id": f"e-{dest}",
        "transition_condition": {"type": "prompt", "prompt": "Always"},
        "destination_node_id": dest,
    }


# Flow A: a_start --Always--> shared (a DEEP node). Flow B: b_start (start), plus a COLLIDING
# node id "shared" that is a deep node. After advancing to "shared" in A then repointing to B,
# the stale cursor "shared" must re-enter at B's start (b_start), not resume at B's "shared".
_FLOW_A = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gemini-2.5-flash"},
    "start_node_id": "a_start",
    "nodes": [
        _convo("a_start", "MARK_A_START", [_always("shared")]),
        _convo("shared", "MARK_A_SHARED"),
    ],
}
_FLOW_B = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gemini-2.5-flash"},
    "start_node_id": "b_start",
    "nodes": [_convo("b_start", "MARK_B_START"), _convo("shared", "MARK_B_SHARED")],
}


def test_repoint_to_different_flow_reenters_at_start_not_colliding_node(
    compat_client, compat_headers, flow_runtime_on, monkeypatch
):
    async def _fake(**kw: Any) -> VertexTurn:
        sysi = kw.get("system_instruction", "")
        for mark in ("MARK_A_START", "MARK_A_SHARED", "MARK_B_START", "MARK_B_SHARED"):
            if mark in sysi:
                return VertexTurn(text=f"reply-from-{mark}")
        return VertexTurn(text="reply-from-REPLY")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _fake)
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _fake)

    flow_a = _create_flow(compat_client, compat_headers, _FLOW_A)
    flow_b = _create_flow(compat_client, compat_headers, _FLOW_B)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_a)
    chat_id = _start_chat(compat_client, compat_headers, agent_id)

    # turn 1: enter a_start; turn 2: Always -> shared (cursor now "<flowA>:shared")
    r1 = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hi"},
        headers=compat_headers,
    )
    assert _last_content(r1.json()) == "reply-from-MARK_A_START"
    r2 = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "ok"},
        headers=compat_headers,
    )
    assert _last_content(r2.json()) == "reply-from-MARK_A_SHARED"

    # repoint the agent to flow B (Phase 6c update-agent, same profile, republished)
    rp = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_b}},
        headers=compat_headers,
    )
    assert rp.status_code == 200, rp.text

    # turn 3: the stale "shared" cursor belongs to flow A, so it must NOT resolve against flow B's
    # same-named node — the session re-enters at flow B's start (MARK_B_START), not MARK_B_SHARED.
    r3 = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "still here"},
        headers=compat_headers,
    )
    assert _last_content(r3.json()) == "reply-from-MARK_B_START"
