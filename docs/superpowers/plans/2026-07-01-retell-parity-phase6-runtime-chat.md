# Phase 6-runtime-chat — Conversation-Flow DAG runtime (chat/SMS) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute a bound conversation-flow DAG turn-by-turn for chat/SMS behind the single `generate_agent_reply` chokepoint, gated by a default-off flag, with whole-session fallback to today's single-prompt path for anything the v1 interpreter can't run.

**Architecture:** A new pure-ish interpreter module `compat/flow_runtime.py` (traverses `conversation`+`end` nodes; routes on prompt/equation/Else/Always edges via `run_vertex_turn`). `chat_service.generate_agent_reply` gains a flag-gated branch `_try_flow_reply` that discovers the agent's flow binding from the **raw** published `version.config` (before `AgentConfig` strips `compat_response_engine`), runs one flow turn, and persists a per-session cursor (`chat_sessions.flow_current_node_id`, migration 0050). Returns `None` to signal fallback — all three call sites (api_chat, SMS reply, inbound-autocreate) inherit it for free.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy async / Alembic / Pydantic settings / google-genai (Vertex via ADC) / pytest (asyncio_mode=auto) + testcontainers Postgres.

## Global Constraints

- **apps/api ONLY.** `services/agent` is untouched — the voice slice is the deferred `6-runtime-voice` follow-up. No `services/agent` import from apps/api (Constitution I).
- **No new compat op, no endpoint.** `KNOWN_GAPS` stays `frozenset()`. Surface-coverage tests unchanged.
- **Single alembic head = `0050`** after this phase (down_revision `0049`).
- **`flow_runtime_enabled` default `False`.** Merged-inert. Flag off ⇒ `generate_agent_reply` runs today's code path byte-for-byte (regression-tested).
- **Whole-session fallback.** `_try_flow_reply` returns `None` for unbound / missing-or-archived / cross-org / malformed / non-runnable — and NEVER raises on a bad binding. A live chat must not break because a flow changed.
- **No conformance surface.** Flow execution is internal; chat response shapes do not change. All existing frozen conformance suites must stay green untouched. Do NOT add `assert_conforms` for the interpreter.
- **RLS.** The flow load is `conversation_flows_repo.get` (RLS-scoped); a cross-org flow id is indistinguishable from absent.
- **Vertex only** via `usan_api.vertex_test.run_vertex_turn` (vertexai=True/ADC — never the Gemini Dev API). No LiveKit, no PHI in logs.
- **CI gate:** `uv run ruff check . && uv run ruff format --check .`, `uv run mypy` (config `files=["src"]` — never `mypy .`), full `uv run pytest`.

---

### Task 1: Settings flag + cursor column + migration 0050 + compose passthrough

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (add the flag near the other feature flags)
- Modify: `apps/api/src/usan_api/db/models.py` (add `flow_current_node_id` to `ChatSession`, after the `to_number` column)
- Create: `apps/api/migrations/versions/0050_chat_flow_cursor.py`
- Modify: `infra/docker-compose.yml` (api service `environment:` map)
- Modify: `infra/.env.prod.example` (ship-inert key)
- Test: `apps/api/tests/test_flow_runtime_migration.py`

**Interfaces:**
- Produces: `Settings.flow_runtime_enabled: bool` (default False); `ChatSession.flow_current_node_id: str | None` column; DB column `chat_sessions.flow_current_node_id TEXT NULL`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_flow_runtime_migration.py`:

```python
"""Phase 6-runtime-chat: the flow flag defaults off and the cursor column exists."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import ChatSession


def test_flow_runtime_flag_defaults_off() -> None:
    from usan_api.settings import get_settings

    assert get_settings().flow_runtime_enabled is False


def test_chat_session_has_flow_cursor_attribute() -> None:
    assert hasattr(ChatSession, "flow_current_node_id")


def test_migration_adds_flow_cursor_column(async_database_url: str) -> None:
    async def _check() -> list[str]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'chat_sessions' "
                        "AND column_name = 'flow_current_node_id'"
                    )
                )
                return [r[0] for r in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_check()) == ["flow_current_node_id"]
```

> If `get_settings()` in the test env doesn't already resolve (it does for the rest of the suite — it's the same accessor the app uses), copy the construction idiom from an existing `tests/test_settings*.py`. The intent: a default Settings has `flow_runtime_enabled is False`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_flow_runtime_migration.py -q`
Expected: FAIL — `flow_runtime_enabled` attribute missing / column absent.

- [ ] **Step 3: Add the settings flag**

In `apps/api/src/usan_api/settings.py`, next to `kb_retrieval_voice_enabled` (~line 287), add:

```python
    # Conversation-flow DAG runtime for chat/SMS (Phase 6-runtime-chat). When on, a chat/SMS
    # agent bound to a RUNNABLE conversation flow executes the flow turn-by-turn instead of the
    # single system-prompt turn; a non-runnable flow falls back to the single-prompt path.
    flow_runtime_enabled: bool = Field(default=False, alias="FLOW_RUNTIME_ENABLED")
```

- [ ] **Step 4: Add the ChatSession column**

In `apps/api/src/usan_api/db/models.py`, inside `class ChatSession`, immediately after the `to_number` column:

```python
    # Phase 6-runtime-chat: the conversation-flow DAG cursor (the id of the node that produced
    # the last agent turn). NULL = not entered / flag-off / unbound / non-runnable. Internal
    # only — never echoed by serialize_chat.
    flow_current_node_id: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 5: Create migration 0050**

Create `apps/api/migrations/versions/0050_chat_flow_cursor.py`:

```python
"""chat_sessions.flow_current_node_id: the conversation-flow DAG cursor (6-runtime-chat).

Additive nullable column on the existing chat_sessions table. The table-level GRANT to
usan_app already covers future columns, so no re-grant is needed. Inert until
FLOW_RUNTIME_ENABLED is set. Owner-DDL (ALTER TABLE runs as the usan owner on deploy).

Revision ID: 0050
Revises: 0049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("flow_current_node_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "flow_current_node_id")
```

- [ ] **Step 6: Thread the flag through compose + prod env example**

In `infra/docker-compose.yml`, the `api` service `environment:` map, add a line matching the existing `KB_RETRIEVAL_ENABLED` style — `FLOW_RUNTIME_ENABLED: ${FLOW_RUNTIME_ENABLED:-false}`. In `infra/.env.prod.example`, add an inert line `FLOW_RUNTIME_ENABLED=false` with a one-line comment. (Per the compose-passthrough gotcha: a new key no-ops on the VM unless it's in BOTH the compose env map AND the VM `.env`.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_flow_runtime_migration.py -q`
Expected: PASS (testcontainers applies migration 0050, column present, flag defaults off).
Then: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean. Confirm single head: `uv run alembic heads` → `0050 (head)`.

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/settings.py apps/api/src/usan_api/db/models.py apps/api/migrations/versions/0050_chat_flow_cursor.py infra/docker-compose.yml infra/.env.prod.example apps/api/tests/test_flow_runtime_migration.py
git commit -m "feat(api): 6-runtime-chat scaffolding — flow_runtime_enabled flag + chat_sessions.flow_current_node_id (migration 0050)"
```

---

### Task 2: The DAG interpreter core — `compat/flow_runtime.py`

**Files:**
- Create: `apps/api/src/usan_api/compat/flow_runtime.py`
- Test: `apps/api/tests/compat/test_flow_runtime_unit.py`

**Interfaces:**
- Consumes: `usan_api.prompt_substitution.substitute`, `usan_api.vertex_test.run_vertex_turn` (mocked in tests via `monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", ...)`), `usan_api.settings.Settings`.
- Produces:
  - `node_by_id(config: dict, node_id: str | None) -> dict | None`
  - `flow_is_runnable(config: Any) -> bool`
  - `async evaluate_transition(node: dict, history: list, values: dict, *, model: str, settings: Settings) -> str | None`
  - `async speak(flow_config: dict, node: dict, values: dict, history: list, *, model: str, temperature: float | None, settings: Settings) -> str`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_flow_runtime_unit.py`:

```python
"""DB-free unit tests for the conversation-flow DAG interpreter (Phase 6-runtime-chat)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from usan_api.compat import flow_runtime
from usan_api.vertex_test import VertexTurn


def _msg(role: str, content: str) -> Any:
    return SimpleNamespace(role=role, content=content)


def _convo_node(node_id: str, text: str, edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
        "n1", "hi",
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
        "n1", "hi",
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
        flow, node, {"first_name": "Ann"}, [_msg("user", "hi")],
        model="gemini-2.5-flash", temperature=0.3, settings=object(),
    )
    assert out == "hello Ann"
    assert "You are a helpful assistant. Ann" in captured["system_instruction"]
    assert "Greet the user." in captured["system_instruction"]
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["tools"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_unit.py -q`
Expected: FAIL — `usan_api.compat.flow_runtime` does not exist.

- [ ] **Step 3: Write the interpreter**

Create `apps/api/src/usan_api/compat/flow_runtime.py`:

```python
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
    except (TypeError, ValueError):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_unit.py -q`
Expected: PASS (all cases). Then `uv run ruff check . && uv run ruff format --check . && uv run mypy` → clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/flow_runtime.py apps/api/tests/compat/test_flow_runtime_unit.py
git commit -m "feat(api): 6-runtime-chat interpreter core — flow_is_runnable + evaluate_transition + speak"
```

---

### Task 3: Wire the flow branch into `generate_agent_reply`

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (imports; add `_flow_model` + `_try_flow_reply`; add the flag-gated branch at the top of `generate_agent_reply`)
- Modify: `apps/api/tests/compat/conftest.py` (add a `flow_runtime_on` fixture mirroring `chat_analysis_on`)
- Test: `apps/api/tests/compat/test_flow_runtime_chat.py`

**Interfaces:**
- Consumes: Task 2's `flow_runtime.*`; `conversation_flows_repo.get`; `agent_profiles_repo.get_profile` / `get_published_config`; `ids.decode_conversation_flow_id`; Phase 6c create-agent conversation-flow binding.
- Produces: flow execution behind `settings.flow_runtime_enabled`; the cursor persisted onto `session.flow_current_node_id` (committed by each caller in its own txn).

- [ ] **Step 1: Write the failing test**

First add the fixture. In `apps/api/tests/compat/conftest.py`, after `chat_analysis_on`, add:

```python
@pytest.fixture
def flow_runtime_on(compat_client: TestClient):
    """Override get_settings on the compat sub-app so the flow runtime runs (flag on +
    gcp_project set). Mirrors chat_analysis_on / gcp_project_set."""
    from usan_api.settings import get_settings as _get_settings

    compat_app = _get_compat_app(compat_client)
    base = _get_settings()

    def _override() -> Settings:
        return base.model_copy(update={"flow_runtime_enabled": True, "gcp_project": "test-project"})

    compat_app.dependency_overrides[_get_settings] = _override
    yield
    compat_app.dependency_overrides.pop(_get_settings, None)
```

Create `apps/api/tests/compat/test_flow_runtime_chat.py`:

```python
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
        {"id": "f1", "type": "function", "tool_id": "t", "tool_type": "local", "wait_for_result": True}
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
    # create-chat-completion returns the new agent message(s); adjust to the actual field name
    # if the router wraps them differently (see routers/chats.py).
    return resp_json["messages"][-1]["content"]


@pytest.fixture
def _spy_vertex(monkeypatch):
    """Return a canned reply that echoes the system instruction's node marker so the test can
    assert which node spoke. Patch BOTH the runtime and the chat_service default path."""
    async def _fake(**kw: Any) -> VertexTurn:
        sysi = kw.get("system_instruction", "")
        marker = "NODE_TWO" if "NODE_TWO" in sysi else "NODE_ONE" if "NODE_ONE" in sysi else "REPLY"
        return VertexTurn(text=f"reply-from-{marker}")

    monkeypatch.setattr("usan_api.compat.flow_runtime.run_vertex_turn", _fake)
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _fake)


def test_flag_on_flow_agent_executes_first_node(
    compat_client, compat_headers, flow_runtime_on, _spy_vertex
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
    compat_client, compat_headers, flow_runtime_on, _spy_vertex
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
    compat_client, compat_headers, flow_runtime_on, _spy_vertex
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


def test_flag_off_ignores_flow_binding(compat_client, compat_headers, gcp_project_set, _spy_vertex):
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
```

> **Confirm the completion response shape** before finalizing: inspect `apps/api/src/usan_api/compat/routers/chats.py` for the `/create-chat-completion` response model and adjust `_last_content` to the actual field (the endpoint returns the new agent message(s) as a `ChatCreateChatCompletionResponse`; if the JSON key isn't `messages`, use the real one). Also confirm `/create-chat`, `/create-conversation-flow`, `/create-agent` return 201 in this suite (they do in the 4a / 6a / 6c tests).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_chat.py -q`
Expected: FAIL — the flow branch is not wired; a flow-bound agent runs the single-prompt path, so `test_flag_on_flow_agent_executes_first_node` gets `reply-from-REPLY` (no NODE marker in the system_instruction).

- [ ] **Step 3: Add imports to `chat_service.py`**

- `from typing import Any` (with the stdlib imports at the top, after `import uuid`).
- `from usan_api.compat import flow_runtime` (with the `from usan_api.compat import ids` group).
- `from usan_api.repositories import conversation_flows as conversation_flows_repo` (with the `from usan_api.repositories import ...` group).

- [ ] **Step 4: Add `_flow_model` and `_try_flow_reply`**

Insert immediately **before** `generate_agent_reply` (before the `async def generate_agent_reply` line) in `chat_service.py`:

```python
def _flow_model(flow_config: dict[str, Any], cfg: AgentConfig) -> str:
    """The flow's own model governs its execution; fall back to the agent's llm model."""
    mc = flow_config.get("model_choice")
    if isinstance(mc, dict):
        model = mc.get("model")
        if isinstance(model, str) and model:
            return model
    return cfg.llm.model


async def _try_flow_reply(db: AsyncSession, settings: Settings, session: ChatSession) -> str | None:
    """Execute ONE conversation-flow turn when the session's agent is bound to a RUNNABLE flow;
    return the reply text, or None to signal whole-session fallback to the single-prompt path.
    Never raises on a bad binding (unbound / missing / archived / cross-org / malformed /
    non-runnable all return None) — a live chat must not break because a flow changed. Reads the
    binding from the RAW published version.config, because AgentConfig(extra="ignore") strips the
    top-level compat_response_engine key that Phase 6c writes."""
    profile = await agent_profiles_repo.get_profile(db, session.agent_profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if version is None:
        return None
    raw = version.config or {}
    engine = raw.get("compat_response_engine")
    if not isinstance(engine, dict) or engine.get("type") != "conversation-flow":
        return None
    token = engine.get("conversation_flow_id")
    if not isinstance(token, str) or not token:
        return None
    try:
        flow_uuid = ids.decode_conversation_flow_id(token)
    except CompatError:
        return None
    flow_row = await conversation_flows_repo.get(db, flow_uuid)
    if flow_row is None:
        return None
    flow_config = flow_row.config or {}
    if not flow_runtime.flow_is_runnable(flow_config):
        return None

    cfg = AgentConfig.model_validate(raw)
    bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
    flow_defaults = flow_config.get("default_dynamic_variables")
    merged_custom: dict[str, object] = {}
    if isinstance(flow_defaults, dict):
        merged_custom.update(flow_defaults)
    merged_custom.update(bare_vars)
    values = build_vars({}, merged_custom, timezone="", now=datetime.now(UTC))
    history = await chats_repo.list_messages(db, session.id)
    model = _flow_model(flow_config, cfg)

    cursor = session.flow_current_node_id
    if cursor is None:
        node = flow_runtime.node_by_id(flow_config, flow_config.get("start_node_id"))
    else:
        current = flow_runtime.node_by_id(flow_config, cursor)
        if current is None:
            # the cursor node was edited out of the flow -> re-enter at start
            node = flow_runtime.node_by_id(flow_config, flow_config.get("start_node_id"))
        else:
            dest = await flow_runtime.evaluate_transition(
                current, history, values, model=model, settings=settings
            )
            node = flow_runtime.node_by_id(flow_config, dest) if dest else current
    if node is None:
        return None  # defensive: start node unresolved despite the runnable guard -> fallback

    reply = await flow_runtime.speak(
        flow_config,
        node,
        values,
        history,
        model=model,
        temperature=cfg.llm.temperature,
        settings=settings,
    )
    session.flow_current_node_id = node.get("id")
    return reply
```

- [ ] **Step 5: Add the flag-gated branch to `generate_agent_reply`**

At the very start of `generate_agent_reply` body (immediately after the docstring, before `cfg = await _load_published_config(...)`), insert:

```python
    if settings.flow_runtime_enabled:
        flow_reply = await _try_flow_reply(db, settings, session)
        if flow_reply is not None:
            return flow_reply
```

Leave the rest of the function unchanged — when the flag is off, or `_try_flow_reply` returns None, execution falls through to today's exact single-prompt path.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_chat.py -q`
Expected: PASS (first-node, advance, non-runnable fallback, flag-off).
Then the chat regression + conformance suites (adjust file names to the actual chat test files present):
`uv run pytest -n0 tests/compat/test_flow_runtime_unit.py tests/compat/ -k "chat" -q` → PASS unchanged.
Then `uv run ruff check . && uv run ruff format --check . && uv run mypy` → clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/chat_service.py apps/api/tests/compat/conftest.py apps/api/tests/compat/test_flow_runtime_chat.py
git commit -m "feat(api): 6-runtime-chat — execute bound flows at the generate_agent_reply chokepoint"
```

---

### Task 4: Operator note

**Files:**
- Create: `docs/deployment/flow-runtime-chat.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the operator note**

Create `docs/deployment/flow-runtime-chat.md`:

```markdown
# Conversation-flow runtime — chat/SMS (Phase 6-runtime-chat)

**Status:** merged, INERT until BOTH the next `v*` tag deploy AND `FLOW_RUNTIME_ENABLED=true`
(merged ≠ deployed; flag default off). Migration 0050 adds one nullable column
(`chat_sessions.flow_current_node_id`); no new compat op, no endpoint, `KNOWN_GAPS` unchanged.

## What it does

When `FLOW_RUNTIME_ENABLED` is on and a chat/SMS agent is bound to a **runnable** conversation
flow (via Phase 6c `response_engine: {type: conversation-flow, ...}`), `generate_agent_reply`
executes the flow DAG turn-by-turn instead of the single system-prompt turn. All three chat
entry points inherit it: create-chat-completion (api_chat), inbound SMS reply, inbound
auto-create.

## Runnable = what v1 executes

Only `conversation` and `end` nodes, routed by `prompt` (LLM-classified), `equation`
(deterministic), `Else`, and `Always` edges. A flow that uses ANY other node type (`function`,
`transfer_call`, `branch`, `sms`, `extract_dynamic_variables`, `agent_swap`, `mcp`, `subagent`,
`code`, `press_digit`, `bridge_transfer`, `cancel_transfer`, `component`) is **not runnable** and
that session runs today's single-prompt path (whole-session fallback). Same for a missing /
archived / cross-org / malformed flow binding. The runtime never breaks a live chat.

## Activation

1. Seed `FLOW_RUNTIME_ENABLED=true` into Secret Manager `usan-prod-env` AND the VM `.env`
   BEFORE the `v*` tag (deploy never re-fetches the secret). Requires `GCP_PROJECT` (Vertex).
2. Cut the tag. Bind an agent to a fully-supported flow and exercise a chat.

## Deferred (not honored)

- The entire **voice** path (`6-runtime-voice`: the services/agent build sites + per-turn hook).
- 13 of 15 node types, tool/function nodes, variable extraction, `global_node_setting` global
  transitions, `flex_mode`, per-node model/voice overrides. `static_text` instructions are
  executed as prompts (not verbatim utterances).
- Auto-ending the chat on an `end` node (ending stays the client's `end-chat` op).
```

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/flow-runtime-chat.md
git commit -m "docs(api): 6-runtime-chat operator note"
```

---

## Final verification (after all tasks)

- [ ] `cd apps/api && uv run pytest` (full suite, parallel) → all pass / prior skip count.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` → clean.
- [ ] `uv run alembic heads` → `0050 (head)` single head.
- [ ] `git grep -n "services/agent" apps/api/src/usan_api/compat/flow_runtime.py apps/api/src/usan_api/compat/chat_service.py` → no cross-app import.
- [ ] Confirm the flag-off path is untouched: `git show` the `generate_agent_reply` diff — only the 4-line flag branch was added above the original body.
