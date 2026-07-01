"""DB-free unit tests for the conversation-flow DAG interpreter (Phase 6-runtime-chat)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from usan_api.compat import flow_runtime
from usan_api.vertex_test import VertexTurn


def _msg(role: str, content: str) -> Any:
    return SimpleNamespace(role=role, content=content)


def _convo_node(
    node_id: str, text: str, edges: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "conversation",
        "instruction": {"type": "prompt", "text": text},
        "edges": edges or [],
    }


def _prompt_edge(dest: str, prompt: str) -> dict[str, Any]:
    return {
        "id": f"e-{dest}",
        "transition_condition": {"type": "prompt", "prompt": prompt},
        "destination_node_id": dest,
    }


def _equation_edge(dest: str, left: str, op: str, right: str) -> dict[str, Any]:
    return {
        "id": f"eq-{dest}",
        "transition_condition": {
            "type": "equation",
            "operator": "||",
            "equations": [{"left": left, "operator": op, "right": right}],
        },
        "destination_node_id": dest,
    }


def _two_node_flow() -> dict[str, Any]:
    return {
        "start_node_id": "n1",
        "global_prompt": "You are a helpful assistant. {{first_name}}",
        "model_choice": {"type": "cascading", "model": "gemini-2.5-flash"},
        "nodes": [
            _convo_node("n1", "Greet the user.", [_prompt_edge("n2", "user is done")]),
            {"id": "n2", "type": "end", "instruction": {"type": "prompt", "text": "Say goodbye."}},
        ],
    }


# ---- flow_is_runnable -------------------------------------------------------


def test_runnable_accepts_two_node_flow() -> None:
    assert flow_runtime.flow_is_runnable(_two_node_flow()) is True


def test_runnable_rejects_unsupported_node_type() -> None:
    flow = _two_node_flow()
    flow["nodes"][0]["type"] = "function"
    assert flow_runtime.flow_is_runnable(flow) is False


def test_runnable_rejects_missing_start() -> None:
    flow = _two_node_flow()
    flow["start_node_id"] = "nope"
    assert flow_runtime.flow_is_runnable(flow) is False


def test_runnable_rejects_dangling_destination() -> None:
    flow = _two_node_flow()
    flow["nodes"][0]["edges"] = [_prompt_edge("ghost", "Always")]
    assert flow_runtime.flow_is_runnable(flow) is False


def test_runnable_rejects_conversation_without_instruction() -> None:
    flow = _two_node_flow()
    del flow["nodes"][0]["instruction"]
    assert flow_runtime.flow_is_runnable(flow) is False


def test_runnable_rejects_bad_edge_condition_type() -> None:
    flow = _two_node_flow()
    flow["nodes"][0]["edges"] = [
        {"id": "e", "transition_condition": {"type": "code"}, "destination_node_id": "n2"}
    ]
    assert flow_runtime.flow_is_runnable(flow) is False


def test_runnable_rejects_empty_nodes() -> None:
    assert flow_runtime.flow_is_runnable({"start_node_id": "x", "nodes": []}) is False


# ---- evaluate_transition ----------------------------------------------------


async def test_always_edge_short_circuits(monkeypatch) -> None:
    called = False

    async def _boom(**_: Any) -> VertexTurn:  # must NOT be called
        nonlocal called
        called = True
        return VertexTurn(text="0")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _boom)
    node = _convo_node("n1", "hi", [_prompt_edge("n2", "Always")])
    dest = await flow_runtime.evaluate_transition(node, [], {}, model="m", settings=object())
    assert dest == "n2"
    assert called is False


async def test_equation_edge_matches_without_vertex(monkeypatch) -> None:
    async def _boom(**_: Any) -> VertexTurn:
        raise AssertionError("classifier must not run when an equation matches")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _boom)
    node = _convo_node("n1", "hi", [_equation_edge("n2", "{{mood}}", "==", "happy")])
    dest = await flow_runtime.evaluate_transition(
        node, [], {"mood": "happy"}, model="m", settings=object()
    )
    assert dest == "n2"


async def test_equation_missing_var_is_false(monkeypatch) -> None:
    async def _none(**_: Any) -> VertexTurn:
        return VertexTurn(text="none")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _none)
    # equation references {{mood}} which is absent -> false; no else -> None
    node = _convo_node("n1", "hi", [_equation_edge("n2", "{{mood}}", "==", "happy")])
    dest = await flow_runtime.evaluate_transition(node, [], {}, model="m", settings=object())
    assert dest is None


async def test_prompt_classifier_picks_index(monkeypatch) -> None:
    async def _idx(**_: Any) -> VertexTurn:
        return VertexTurn(text="1")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _idx)
    node = _convo_node(
        "n1",
        "hi",
        [_prompt_edge("a", "wants sales"), _prompt_edge("b", "wants support")],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [_msg("user", "help me")], {}, model="m", settings=object()
    )
    assert dest == "b"


async def test_prompt_none_falls_to_else(monkeypatch) -> None:
    async def _none(**_: Any) -> VertexTurn:
        return VertexTurn(text="none")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _none)
    node = _convo_node(
        "n1",
        "hi",
        [_prompt_edge("a", "wants sales"), _prompt_edge("z", "Else")],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [_msg("user", "??")], {}, model="m", settings=object()
    )
    assert dest == "z"


async def test_no_edges_returns_none() -> None:
    node = {"id": "n2", "type": "end", "instruction": {"type": "prompt", "text": "bye"}}
    dest = await flow_runtime.evaluate_transition(node, [], {}, model="m", settings=object())
    assert dest is None


# ---- speak ------------------------------------------------------------------


async def test_speak_assembles_global_and_node_prompt(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _capture(**kw: Any) -> VertexTurn:
        captured.update(kw)
        return VertexTurn(text="hello Ann")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _capture)
    flow = _two_node_flow()
    node = flow["nodes"][0]
    out = await flow_runtime.speak(
        flow,
        node,
        {"first_name": "Ann"},
        [_msg("user", "hi")],
        model="gemini-2.5-flash",
        temperature=0.3,
        settings=object(),
    )
    assert out == "hello Ann"
    assert "You are a helpful assistant. Ann" in captured["system_instruction"]
    assert "Greet the user." in captured["system_instruction"]
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["tools"] == []
