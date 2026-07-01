"""Conversation-flow DAG runtime for chat/SMS (Phase 6-runtime-chat).

Executes a RetellAI conversation flow turn-by-turn. v1 honors ONLY `conversation` and `end`
nodes routed by prompt/equation/Else/Always edges; a flow using anything else is NOT runnable
(`flow_is_runnable` -> False) and the caller falls back to the single-prompt path
(whole-session fallback). Text-only Vertex via `run_vertex_turn` — no LiveKit, no
services/agent import. Never logs PHI; the functions here raise only if Vertex itself raises
(the caller owns that path, identical to today's generate_agent_reply).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from usan_api.prompt_substitution import substitute
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn

_SUPPORTED_NODE_TYPES = frozenset({"conversation", "end"})
_ALWAYS = "Always"
_ELSE = "Else"
_INT_RE = re.compile(r"\d+")


def node_by_id(config: Mapping[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for node in config.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    return None


def _instruction_text(node: Mapping[str, Any]) -> str | None:
    instr = node.get("instruction")
    if isinstance(instr, dict):
        text = instr.get("text")
        return text if isinstance(text, str) else None
    return None


def flow_is_runnable(config: Any) -> bool:
    """True iff v1 can execute the whole flow: start node resolves, every node is a
    conversation/end node (conversation nodes carry a readable instruction), every edge
    condition is prompt|equation, and no edge points at a non-existent node."""
    if not isinstance(config, Mapping):
        return False
    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    ids_seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            return False
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            return False
        if node.get("type") not in _SUPPORTED_NODE_TYPES:
            return False
        ids_seen.add(node_id)
    start = config.get("start_node_id")
    if not isinstance(start, str) or start not in ids_seen:
        return False
    for node in nodes:
        if node.get("type") == "conversation" and _instruction_text(node) is None:
            return False
        for edge in node.get("edges") or []:
            if not isinstance(edge, dict):
                return False
            cond = edge.get("transition_condition")
            if not isinstance(cond, dict) or cond.get("type") not in ("prompt", "equation"):
                return False
            dest = edge.get("destination_node_id")
            if dest is not None and dest not in ids_seen:
                return False
    return True


def _coerce_number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except TypeError, ValueError:
        return None


def _equation_true(eq: Mapping[str, Any], values: Mapping[str, str]) -> bool:
    op = eq.get("operator")
    left_raw = eq.get("left")
    right_raw = eq.get("right")
    left = substitute(left_raw, values) if isinstance(left_raw, str) else str(left_raw)
    right = substitute(right_raw, values) if isinstance(right_raw, str) else str(right_raw)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "contains":
        return right in left
    left_num, right_num = _coerce_number(left), _coerce_number(right)
    if left_num is None or right_num is None:
        return False
    if op == ">":
        return left_num > right_num
    if op == ">=":
        return left_num >= right_num
    if op == "<":
        return left_num < right_num
    if op == "<=":
        return left_num <= right_num
    return False


def _equation_condition_true(cond: Mapping[str, Any], values: Mapping[str, str]) -> bool:
    eqs = [e for e in (cond.get("equations") or []) if isinstance(e, dict)]
    if not eqs:
        return False
    results = [_equation_true(e, values) for e in eqs]
    return all(results) if cond.get("operator") == "&&" else any(results)


def _contents(history: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in history
    ]


def _cond(edge: Mapping[str, Any]) -> dict[str, Any]:
    cond = edge.get("transition_condition")
    return cond if isinstance(cond, dict) else {}


def _is_prompt(edge: Mapping[str, Any], *, prompt: str | None = None) -> bool:
    cond = _cond(edge)
    if cond.get("type") != "prompt":
        return False
    return prompt is None or cond.get("prompt") == prompt


async def _classify(
    prompt_edges: Sequence[Mapping[str, Any]],
    history: Sequence[Any],
    values: Mapping[str, str],
    *,
    model: str,
    settings: Settings,
) -> int | None:
    lines = [
        f"{i}: {substitute(str(_cond(e).get('prompt') or ''), values)}"
        for i, e in enumerate(prompt_edges)
    ]
    system_instruction = (
        "You are a conversation-flow routing classifier. Given the conversation so far, "
        "choose which ONE of the numbered transition conditions is now satisfied. "
        "Reply with ONLY the number, or the word 'none' if none applies.\n\n"
        "Transition conditions:\n" + "\n".join(lines)
    )
    turn = await run_vertex_turn(
        model=model,
        temperature=0.0,
        system_instruction=system_instruction,
        tools=[],
        contents=_contents(history),
        settings=settings,
    )
    match = _INT_RE.search(turn.text or "")
    return int(match.group()) if match else None


async def evaluate_transition(
    node: Mapping[str, Any],
    history: Sequence[Any],
    values: Mapping[str, str],
    *,
    model: str,
    settings: Settings,
) -> str | None:
    """Pick the next destination_node_id: Always > satisfied equation > LLM-classified prompt
    edge > Else. Returns None when nothing matches (caller remains on the current node)."""
    edges = [e for e in (node.get("edges") or []) if isinstance(e, dict)]
    for edge in edges:
        if _is_prompt(edge, prompt=_ALWAYS):
            return edge.get("destination_node_id")
    for edge in edges:
        if _cond(edge).get("type") == "equation" and _equation_condition_true(_cond(edge), values):
            return edge.get("destination_node_id")
    prompt_edges = [
        e for e in edges if _is_prompt(e) and _cond(e).get("prompt") not in (_ALWAYS, _ELSE)
    ]
    else_edge = next((e for e in edges if _is_prompt(e, prompt=_ELSE)), None)
    if prompt_edges:
        idx = await _classify(prompt_edges, history, values, model=model, settings=settings)
        if idx is not None and 0 <= idx < len(prompt_edges):
            return prompt_edges[idx].get("destination_node_id")
    if else_edge is not None:
        return else_edge.get("destination_node_id")
    return None


async def speak(
    flow_config: Mapping[str, Any],
    node: Mapping[str, Any],
    values: Mapping[str, str],
    history: Sequence[Any],
    *,
    model: str,
    temperature: float | None,
    settings: Settings,
) -> str:
    """Run one Vertex turn for the node: system = global_prompt + node instruction (both
    var-substituted), contents = the role-mapped history. Returns the reply text."""
    node_instruction = substitute(_instruction_text(node) or "", values)
    global_prompt = substitute(str(flow_config.get("global_prompt") or ""), values)
    system_instruction = f"{global_prompt}\n\n{node_instruction}".strip()
    turn = await run_vertex_turn(
        model=model,
        temperature=temperature,
        system_instruction=system_instruction,
        tools=[],
        contents=_contents(history),
        settings=settings,
    )
    return turn.text
