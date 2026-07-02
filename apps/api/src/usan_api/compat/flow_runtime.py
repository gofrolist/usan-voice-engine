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
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.prompt_substitution import build_vars, substitute
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


def node_instruction_text(node: Mapping[str, Any]) -> str | None:
    instr = node.get("instruction")
    if isinstance(instr, dict):
        text = instr.get("text")
        return text if isinstance(text, str) else None
    return None


def bound_flow_id(raw: Mapping[str, Any]) -> uuid.UUID | None:
    """The conversation_flow uuid this published agent config is bound to (Phase 6c
    compat_response_engine), or None if unbound / non-flow / malformed. Never raises."""
    engine = raw.get("compat_response_engine")
    if not isinstance(engine, dict) or engine.get("type") != "conversation-flow":
        return None
    token = engine.get("conversation_flow_id")
    if not isinstance(token, str) or not token:
        return None
    try:
        return ids.decode_conversation_flow_id(token)
    except CompatError:
        return None


def make_cursor(flow_uuid: uuid.UUID, flow_version: int, node_id: str | None) -> str:
    """Encode the opaque, flow-qualified cursor token round-tripped by chat and voice
    callers: ``"<flow_uuid>:<flow_version>:<node_id>"``. The caller never interprets it."""
    return f"{flow_uuid}:{flow_version}:{node_id}"


def cursor_for_flow(stored: str | None, flow_uuid: uuid.UUID, flow_version: int) -> str | None:
    """The node id from a stored ``"<flow_uuid>:<flow_version>:<node_id>"`` cursor, but ONLY if it
    belongs to the currently-bound flow AT THE SAME VERSION; otherwise None (re-enter at start).

    Qualifying by flow uuid alone is not enough: conversation_flows.update() mutates the flow row
    in place and bumps ``version``, so a node id valid before an edit may point at a semantically
    different node after it. A version mismatch (or a legacy 2-part cursor written before this
    change) safely re-enters at start rather than resuming on the wrong node."""
    if not stored:
        return None
    parts = stored.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, version, node_id = parts
    if prefix != str(flow_uuid) or version != str(flow_version):
        return None
    return node_id or None


def assemble_instruction(
    flow_config: Mapping[str, Any], node: Mapping[str, Any], values: Mapping[str, str]
) -> str:
    """The system instruction for one node: global_prompt + node instruction, both
    var-substituted and joined by a blank line, stripped. Shared by chat's ``speak`` and the
    voice resolver so the two paths can never diverge on prompt assembly."""
    global_prompt = substitute(str(flow_config.get("global_prompt") or ""), values)
    node_text = substitute(node_instruction_text(node) or "", values)
    return f"{global_prompt}\n\n{node_text}".strip()


def merge_flow_values(
    flow_config: Mapping[str, Any], dynamic_vars: Mapping[str, object]
) -> dict[str, str]:
    """The rendered var map for a flow turn: the flow's own ``default_dynamic_variables`` (when
    present) updated by the caller's ``dynamic_vars`` (session/call personalization wins),
    rendered via :func:`build_vars` with an empty timezone and "now" (flows are text/voice
    context, not clock-rendering SMS greetings). Shared by chat's ``_try_flow_reply`` and the
    voice resolver so the merge order can never diverge."""
    merged_custom: dict[str, object] = {}
    flow_defaults = flow_config.get("default_dynamic_variables")
    if isinstance(flow_defaults, dict):
        merged_custom.update(flow_defaults)
    merged_custom.update(dynamic_vars)
    return build_vars({}, merged_custom, timezone="", now=datetime.now(UTC))


def flow_model(flow_config: Mapping[str, Any], fallback_model: str) -> str:
    """The flow's own model governs its execution; fall back to the agent's llm model."""
    mc = flow_config.get("model_choice")
    if isinstance(mc, dict):
        model = mc.get("model")
        if isinstance(model, str) and model:
            return model
    return fallback_model


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
        if node.get("type") == "conversation" and node_instruction_text(node) is None:
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
    if op == "not_contains":
        return right not in left
    # exists / not_exist test presence of a value. substitute() renders a missing var to "",
    # so a non-empty substituted `left` means the referenced variable resolved to a value.
    if op == "exists":
        return bool(left.strip())
    if op == "not_exist":
        return not left.strip()
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


def history_to_contents(history: Sequence[Any]) -> list[dict[str, Any]]:
    """Map chat messages to the genai `contents` shape: "agent" turns become the model role,
    every other role ("user"/"sms") becomes user. Shared by the flow runtime and the
    single-prompt path so the two can never diverge on how turns reach Vertex."""
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
        contents=history_to_contents(history),
        settings=settings,
    )
    # The classifier is instructed to reply with ONLY the index or "none". Parse conservatively:
    # a leading "none" (incl. "None of the ... apply") is the negative case, and the index must be
    # anchored at the start — a stray digit buried in prose ("None of the 3 apply") must NOT be
    # mistaken for a chosen index. A non-conforming reply yields None -> the caller falls to Else.
    text = (turn.text or "").strip()
    if text[:4].lower() == "none":
        return None
    match = _INT_RE.match(text)
    return int(match.group()) if match else None


async def evaluate_transition(
    node: Mapping[str, Any],
    history: Sequence[Any],
    values: Mapping[str, str],
    *,
    model: str,
    settings: Settings,
) -> str | None:
    """Pick the next destination_node_id: satisfied equation > Always > LLM-classified prompt
    edge > Else. Returns None when nothing matches (caller remains on the current node).

    Equation edges are evaluated BEFORE Always: an equation is an explicit condition, whereas an
    Always edge is an unconditional catch-all, so a satisfied condition must win over the
    fallback even when the Always edge appears first in the array.

    A terminal ``end`` node never routes onward, even if authored with a stray edge: guard first
    so a misconfigured Always/equation edge on an end node can never advance the flow."""
    if node.get("type") == "end":
        return None
    edges = [e for e in (node.get("edges") or []) if isinstance(e, dict)]
    for edge in edges:
        if _cond(edge).get("type") == "equation" and _equation_condition_true(_cond(edge), values):
            return edge.get("destination_node_id")
    for edge in edges:
        if _is_prompt(edge, prompt=_ALWAYS):
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
    system_instruction = assemble_instruction(flow_config, node, values)
    turn = await run_vertex_turn(
        model=model,
        temperature=temperature,
        system_instruction=system_instruction,
        tools=[],
        contents=history_to_contents(history),
        settings=settings,
    )
    return turn.text
