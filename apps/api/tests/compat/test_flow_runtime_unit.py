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


async def test_satisfied_equation_beats_always_regardless_of_order(monkeypatch) -> None:
    # A satisfied equation must win over an unconditional Always catch-all even when Always
    # appears first in the array (equation = explicit condition; Always = fallback).
    async def _boom(**_: Any) -> VertexTurn:
        raise AssertionError("no classifier turn expected")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _boom)
    node = _convo_node(
        "n1",
        "hi",
        [_prompt_edge("retry", "Always"), _equation_edge("pass", "{{score}}", ">=", "80")],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [], {"score": "90"}, model="m", settings=object()
    )
    assert dest == "pass"


async def test_always_wins_when_equation_not_satisfied(monkeypatch) -> None:
    async def _boom(**_: Any) -> VertexTurn:
        raise AssertionError("no classifier turn expected")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _boom)
    node = _convo_node(
        "n1",
        "hi",
        [_prompt_edge("retry", "Always"), _equation_edge("pass", "{{score}}", ">=", "80")],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [], {"score": "10"}, model="m", settings=object()
    )
    assert dest == "retry"


async def test_classifier_none_prose_does_not_misparse_as_index(monkeypatch) -> None:
    # The model is told to reply "none"; a verbose negative reply that happens to contain a digit
    # ("None of the 3 conditions apply") must NOT be read as index 3 -> falls to Else, not edge 3.
    async def _prose(**_: Any) -> VertexTurn:
        return VertexTurn(text="None of the 3 conditions apply.")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _prose)
    node = _convo_node(
        "n1",
        "hi",
        [
            _prompt_edge("a", "wants sales"),
            _prompt_edge("b", "wants support"),
            _prompt_edge("c", "wants billing"),
            _prompt_edge("z", "Else"),
        ],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [_msg("user", "hmm")], {}, model="m", settings=object()
    )
    assert dest == "z"


async def test_classifier_verbose_number_falls_to_else(monkeypatch) -> None:
    # A non-conforming reply whose index is not anchored at the start ("I pick 0") is not trusted
    # (could be misparsed) -> falls to Else rather than risk routing to a wrong-but-in-range node.
    async def _verbose(**_: Any) -> VertexTurn:
        return VertexTurn(text="I pick 0")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _verbose)
    node = _convo_node(
        "n1",
        "hi",
        [_prompt_edge("a", "wants sales"), _prompt_edge("z", "Else")],
    )
    dest = await flow_runtime.evaluate_transition(
        node, [_msg("user", "hmm")], {}, model="m", settings=object()
    )
    assert dest == "z"


async def test_classifier_bare_index_still_routes(monkeypatch) -> None:
    async def _idx(**_: Any) -> VertexTurn:
        return VertexTurn(text="0")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _idx)
    node = _convo_node("n1", "hi", [_prompt_edge("a", "wants sales"), _prompt_edge("z", "Else")])
    dest = await flow_runtime.evaluate_transition(
        node, [_msg("user", "sales please")], {}, model="m", settings=object()
    )
    assert dest == "a"


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
