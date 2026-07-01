# Phase 6-runtime-voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute a bound RetellAI conversation-flow DAG turn-by-turn on a live voice call by steering the agent's active system prompt, reusing the existing `apps/api` interpreter over one new HTTP endpoint.

**Architecture:** The flow controls which node's system prompt is active; LiveKit keeps owning STT/LLM/TTS/turn-detection/barge-in/tools. `apps/api/.../compat/flow_runtime.py` (built in 6-runtime-chat) stays the single interpreter; `services/agent` reaches it only over HTTP through a new `POST /v1/runtime/flow-advance`. The agent holds the flow cursor (no migration). Flag-gated `FLOW_RUNTIME_VOICE_ENABLED`, default off, whole-session fallback.

**Tech Stack:** FastAPI (Python 3.14, uv, apps/api), LiveKit Agents 1.x (Python 3.12, uv, services/agent), Pydantic v2, Vertex AI (BAA-covered), pytest (`asyncio_mode="auto"` both suites), ruff, mypy.

## Global Constraints

- **No cross-app import:** `apps/api` and `services/agent` never import each other. The interpreter stays in `apps/api`; the agent reaches it only via HTTP.
- **PHI-free logs:** log only `type(exc).__name__` and counts — never turn/instruction/variable content.
- **Best-effort agent paths:** every new agent path is exception-guarded; an advance failure leaves the agent on the current node and never aborts a turn or crashes a call.
- **RLS / org isolation:** a cross-org or malformed flow binding is indistinguishable from "no flow" (`bound=false`), never an error.
- **Vertex only:** the transition classifier runs via `run_vertex_turn` (the existing `apps/api` Vertex path); never a non-BAA LLM.
- **Flag default off:** `FLOW_RUNTIME_VOICE_ENABLED` defaults `False` on both sides; the feature ships inert.
- **DRY interpreter:** do not duplicate DAG-interpretation logic; reuse `flow_runtime`.
- **apps/api:** ruff line-length 100, target py314, `uv run mypy` with `files=["src"]` (run `uv run mypy`, never `mypy .`). Tests: `uv run pytest` (parallel) or `uv run pytest -n0` (serial).
- **services/agent:** ruff line-length 100 target py312 (`select` includes `S`, `PT`, `RET`, `SIM`, `ASYNC`; tests ignore `S`), `uv run mypy` strict `files=["src"]`. Tests: `uv run pytest -v`.
- **Commit format:** `type(scope): description`, scopes `api` / `agent` / `infra` / `docs`.

---

## Task 1: Promote shared flow-binding helpers + api-side flag

Extract the flow-binding helpers `_bound_flow_id` and `_flow_model` (and the instruction-text reader) out of `compat/chat_service.py` into the shared `compat/flow_runtime.py` so the new voice resolver can reuse them without duplication, and add the api-side feature flag. Chat behavior is unchanged (pure delegation).

**Files:**
- Modify: `apps/api/src/usan_api/compat/flow_runtime.py`
- Modify: `apps/api/src/usan_api/compat/chat_service.py`
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/compat/test_flow_runtime_unit.py` (extend)

**Interfaces:**
- Produces (in `flow_runtime`):
  - `def bound_flow_id(raw: Mapping[str, Any]) -> uuid.UUID | None` — the conversation_flow uuid a published config is bound to, or None (unbound / non-flow / malformed). Never raises.
  - `def flow_model(flow_config: Mapping[str, Any], fallback_model: str) -> str` — the flow's `model_choice.model` else `fallback_model`.
  - `def node_instruction_text(node: Mapping[str, Any]) -> str | None` — the node's instruction text (renames the current private `_instruction_text`, kept as a public function).
- Produces (in `settings`): `flow_runtime_voice_enabled: bool` (alias `FLOW_RUNTIME_VOICE_ENABLED`, default False).

- [ ] **Step 1: Write failing tests for the promoted helpers**

Add to `apps/api/tests/compat/test_flow_runtime_unit.py`:

```python
import uuid


def test_bound_flow_id_decodes_conversation_flow_engine() -> None:
    from usan_api.compat import ids

    fid = uuid.uuid4()
    raw = {
        "compat_response_engine": {
            "type": "conversation-flow",
            "conversation_flow_id": ids.encode_conversation_flow_id(fid),
        }
    }
    assert flow_runtime.bound_flow_id(raw) == fid


def test_bound_flow_id_none_when_unbound_or_malformed() -> None:
    assert flow_runtime.bound_flow_id({}) is None
    assert flow_runtime.bound_flow_id({"compat_response_engine": {"type": "retell-llm"}}) is None
    assert flow_runtime.bound_flow_id(
        {"compat_response_engine": {"type": "conversation-flow", "conversation_flow_id": "bad"}}
    ) is None


def test_flow_model_prefers_flow_choice_then_fallback() -> None:
    assert flow_runtime.flow_model({"model_choice": {"model": "gemini-x"}}, "fb") == "gemini-x"
    assert flow_runtime.flow_model({}, "fb") == "fb"
    assert flow_runtime.flow_model({"model_choice": {}}, "fb") == "fb"


def test_node_instruction_text_reads_prompt() -> None:
    node = {"id": "n1", "type": "conversation", "instruction": {"type": "prompt", "text": "Hi"}}
    assert flow_runtime.node_instruction_text(node) == "Hi"
    assert flow_runtime.node_instruction_text({"id": "n2", "type": "end"}) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_unit.py -k "bound_flow_id or flow_model or node_instruction_text" -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'bound_flow_id'`.

- [ ] **Step 3: Add the helpers to `flow_runtime.py`**

At the top of `apps/api/src/usan_api/compat/flow_runtime.py`, add these imports beside the existing ones:

```python
import uuid

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
```

Rename the existing private `_instruction_text` to a public `node_instruction_text` (update its internal callers — currently `flow_is_runnable` and `speak` both call `_instruction_text(node)`), so the function body is unchanged but the name is public:

```python
def node_instruction_text(node: Mapping[str, Any]) -> str | None:
    instr = node.get("instruction")
    if isinstance(instr, dict):
        text = instr.get("text")
        return text if isinstance(text, str) else None
    return None
```

In `flow_is_runnable`, change `_instruction_text(node)` to `node_instruction_text(node)`. In `speak`, change `_instruction_text(node)` to `node_instruction_text(node)`.

Add the two new helpers (place them after `node_by_id`):

```python
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


def flow_model(flow_config: Mapping[str, Any], fallback_model: str) -> str:
    """The flow's own model governs its execution; fall back to the agent's llm model."""
    mc = flow_config.get("model_choice")
    if isinstance(mc, dict):
        model = mc.get("model")
        if isinstance(model, str) and model:
            return model
    return fallback_model
```

- [ ] **Step 4: Delegate from `chat_service.py`**

In `apps/api/src/usan_api/compat/chat_service.py`, delete the local `_bound_flow_id` and `_flow_model` definitions, and update their call sites (both in `_try_flow_reply`):
- `model = _flow_model(flow_config, cfg)` → `model = flow_runtime.flow_model(flow_config, cfg.llm.model)`.
- `flow_uuid = _bound_flow_id(raw)` → `flow_uuid = flow_runtime.bound_flow_id(raw)`.

`chat_service` already imports `flow_runtime`; leave its other imports untouched.

- [ ] **Step 5: Add the api-side flag to `settings.py`**

In `apps/api/src/usan_api/settings.py`, immediately after the `flow_runtime_enabled` field (the `FLOW_RUNTIME_ENABLED` line), add:

```python
    # Conversation-flow DAG runtime for VOICE calls (Phase 6-runtime-voice). When on, a voice
    # agent bound to a RUNNABLE conversation flow is steered node-by-node via
    # POST /v1/runtime/flow-advance; a non-runnable/absent binding falls back to the single
    # static prompt. Independent of flow_runtime_enabled (chat) so voice and chat enable separately.
    flow_runtime_voice_enabled: bool = Field(default=False, alias="FLOW_RUNTIME_VOICE_ENABLED")
```

- [ ] **Step 6: Run helper + chat regression tests**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_flow_runtime_unit.py tests/compat/test_flow_runtime_chat.py -q`
Expected: PASS (new helper tests pass; chat flow tests still green after delegation).

- [ ] **Step 7: Lint + type**

Run: `cd apps/api && ruff check src/usan_api/compat/flow_runtime.py src/usan_api/compat/chat_service.py src/usan_api/settings.py && uv run mypy`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/compat/flow_runtime.py apps/api/src/usan_api/compat/chat_service.py apps/api/src/usan_api/settings.py apps/api/tests/compat/test_flow_runtime_unit.py
git commit -m "refactor(api): promote flow-binding helpers to flow_runtime + add voice flag"
```

---

## Task 2: Voice flow resolver + `/v1/runtime/flow-advance` endpoint

The core apps/api deliverable: a call-oriented resolver that finds a call's bound runnable flow and computes the next node, plus the worker-token endpoint the agent calls each turn.

**Files:**
- Create: `apps/api/src/usan_api/schemas/runtime.py`
- Create: `apps/api/src/usan_api/compat/flow_runtime_voice.py`
- Modify: `apps/api/src/usan_api/routers/runtime.py`
- Test: `apps/api/tests/test_flow_runtime_voice.py`

**Interfaces:**
- Consumes: `flow_runtime.{bound_flow_id, flow_model, flow_is_runnable, node_by_id, evaluate_transition, node_instruction_text}`; `agent_profiles_repo.{resolve_agent_config, get_profile, get_published_config}`; `calls_repo.get_call`; `contacts_repo.get_contact`; `conversation_flows_repo.get`; `prompt_substitution.{build_vars, substitute}`; `db.base.CallDirection`.
- Produces:
  - `schemas/runtime.py`: `FlowTurn{role: str, content: str}`, `FlowAdvanceRequest{call_id: uuid.UUID, current_node_id: str | None = None, turns: list[FlowTurn] = []}`, `FlowAdvanceResponse{bound: bool, node_id: str | None = None, instruction: str | None = None, is_end: bool = False}`.
  - `flow_runtime_voice.py`: `async resolve_bound_flow(db, settings, call_id) -> tuple[dict, dict[str, str], str] | None` (flow_config, values, model); `async advance(db, settings, call_id, current_node_id, turns) -> FlowAdvanceResponse`.
  - `POST /v1/runtime/flow-advance` (worker-token; flag-gated).

- [ ] **Step 1: Write the request/response schemas**

Create `apps/api/src/usan_api/schemas/runtime.py`:

```python
"""Runtime worker-facing schemas (Phase 6-runtime-voice)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class FlowTurn(BaseModel):
    # role is already mapped by the agent: "agent" for the assistant, "user" otherwise.
    # Attribute access (.role/.content) matches the duck type flow_runtime.evaluate_transition
    # consumes, so a list[FlowTurn] is passed straight through as conversation history.
    role: str
    content: str


class FlowAdvanceRequest(BaseModel):
    call_id: uuid.UUID
    current_node_id: str | None = None
    turns: list[FlowTurn] = Field(default_factory=list)


class FlowAdvanceResponse(BaseModel):
    bound: bool
    node_id: str | None = None
    instruction: str | None = None
    is_end: bool = False
```

- [ ] **Step 2: Write failing endpoint tests**

Create `apps/api/tests/test_flow_runtime_voice.py`. This mirrors `tests/test_runtime.py`'s worker-token + seeding idiom. It seeds an org, a published voice profile whose raw config carries a `compat_response_engine` binding, a conversation flow, and an outbound call, then drives the endpoint.

```python
from __future__ import annotations

import asyncio
import time
import uuid

import jwt

from usan_api.compat import ids

_SECRET = "s" * 32


def _worker_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, _SECRET, algorithm="HS256"
    )


def _wauth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_worker_token()}"}


def _two_node_flow() -> dict:
    return {
        "start_node_id": "n1",
        "global_prompt": "You are Ann's assistant.",
        "nodes": [
            {
                "id": "n1",
                "type": "conversation",
                "instruction": {"type": "prompt", "text": "Greet the caller."},
                "edges": [
                    {
                        "id": "e1",
                        "transition_condition": {"type": "prompt", "prompt": "Always"},
                        "destination_node_id": "n2",
                    }
                ],
            },
            {"id": "n2", "type": "end", "instruction": {"type": "prompt", "text": "Say goodbye."}},
        ],
    }


async def _seed(async_url: str, *, flow_config: dict, bind: bool, same_org: bool = True) -> dict:
    """Seed org+profile(+binding)+flow+call. Returns ids as strings.

    Wire this against the project's existing seeding helpers used in tests/test_runtime.py and
    the tests/compat fixtures (org create, agent_profiles_repo publish a version whose raw
    config carries the binding, conversation_flows_repo.create, calls_repo.create_call).
    bind=True writes compat_response_engine into the published version.config; same_org=False
    stores the flow under a different org to exercise RLS isolation.
    """
    ...


def _run(coro):
    return asyncio.run(coro)


def test_flag_off_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "false")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.status_code == 200
    assert resp.json()["bound"] is False


def test_bound_enters_start_node(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    body = resp.json()
    assert body["bound"] is True
    assert body["node_id"] == "n1"
    assert "Greet the caller." in body["instruction"]
    assert "You are Ann's assistant." in body["instruction"]
    assert body["is_end"] is False


def test_always_edge_advances_to_end(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={
            "call_id": seeded["call_id"],
            "current_node_id": "n1",
            "turns": [{"role": "user", "content": "ok bye"}],
        },
        headers=_wauth(),
    )
    body = resp.json()
    assert body["bound"] is True
    assert body["node_id"] == "n2"
    assert body["is_end"] is True


def test_stale_cursor_reenters_start(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": "ghost", "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["node_id"] == "n1"


def test_unbound_call_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=False))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_unrunnable_flow_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    flow = _two_node_flow()
    flow["nodes"][0]["type"] = "function"  # unsupported node type -> not runnable
    seeded = _run(_seed(async_database_url, flow_config=flow, bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_cross_org_flow_binding_is_unbound(client, async_database_url, monkeypatch) -> None:
    # The flow lives under a DIFFERENT org than the call: RLS makes it indistinguishable
    # from absent -> bound=False (never leaks, never errors).
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(
        _seed(async_database_url, flow_config=_two_node_flow(), bind=True, same_org=False)
    )
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_missing_call_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": str(uuid.uuid4()), "current_node_id": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_requires_worker_token(client, async_database_url) -> None:
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": str(uuid.uuid4()), "current_node_id": None, "turns": []},
    )
    assert resp.status_code == 401
```

> **Implementer note on `_seed`:** wire it against the exact seeding helpers already used by `apps/api/tests/test_runtime.py` and the `apps/api/tests/compat/` fixtures (they publish a profile version and create conversation flows + calls). The binding is written by putting `{"compat_response_engine": {"type": "conversation-flow", "conversation_flow_id": ids.encode_conversation_flow_id(<flow_uuid>)}}` into the published version's raw `config` dict alongside a minimal valid `AgentConfig` payload (reuse the compat fixtures' published-config builder). For `same_org=False`, create the conversation flow under a second org context so RLS hides it from the call's org. Follow `test_runtime.py`'s `create_async_engine(async_url, poolclass=NullPool)` + `async_sessionmaker(expire_on_commit=False)` pattern driven by `asyncio.run`. If a per-test `FLOW_RUNTIME_VOICE_ENABLED` env change is not observed by the app's cached `get_settings()`, follow the settings-override idiom used elsewhere in the suite (e.g. `tests/test_settings.py` / the compat conftest `flow_runtime_on` fixture) instead of `monkeypatch.setenv`.

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/api && uv run pytest -n0 tests/test_flow_runtime_voice.py -q`
Expected: FAIL (endpoint 404 / module missing).

- [ ] **Step 4: Implement the resolver**

Create `apps/api/src/usan_api/compat/flow_runtime_voice.py`:

```python
"""Call-oriented conversation-flow resolver for the VOICE runtime (Phase 6-runtime-voice).

Resolves a call's bound RUNNABLE flow and computes the node the conversation should be on,
reusing the shared interpreter in flow_runtime. No speak() — voice never generates speech
server-side; the agent's LiveKit LLM speaks under the returned instruction. Never raises for
flow-shape reasons: an unbound / missing / cross-org / non-runnable binding is bound=False.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import flow_runtime
from usan_api.db.base import CallDirection
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import conversation_flows as conversation_flows_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.schemas.runtime import FlowAdvanceResponse, FlowTurn
from usan_api.settings import Settings

_UNBOUND = FlowAdvanceResponse(bound=False)


async def resolve_bound_flow(
    db: AsyncSession, settings: Settings, call_id: uuid.UUID
) -> tuple[dict, dict[str, str], str] | None:
    """(flow_config, values, model) for the call's bound RUNNABLE flow, else None.

    Resolves the call -> its winning published agent version (same precedence the
    /agent-config endpoint uses), reads the RAW version.config (AgentConfig(extra="ignore")
    would strip compat_response_engine), decodes the flow binding, loads the org-scoped flow
    (RLS: cross-org id -> None), and returns None unless flow_is_runnable."""
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        return None
    contact_profile_id: uuid.UUID | None = None
    if call.contact_id is not None:
        contact = await contacts_repo.get_contact(db, call.contact_id)
        if contact is not None:
            contact_profile_id = contact.agent_profile_id
    resolved_direction = "outbound" if call.direction is CallDirection.OUTBOUND else "inbound"
    resolved = await agent_profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact_profile_id,
        direction=resolved_direction,
    )
    if resolved is None or resolved.profile_id is None:
        return None
    profile = await agent_profiles_repo.get_profile(db, resolved.profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if version is None:
        return None
    raw = version.config or {}

    flow_uuid = flow_runtime.bound_flow_id(raw)
    if flow_uuid is None:
        return None
    flow_row = await conversation_flows_repo.get(db, flow_uuid)
    if flow_row is None:
        return None
    flow_config = flow_row.config or {}
    if not flow_runtime.flow_is_runnable(flow_config):
        return None

    cfg = AgentConfig.model_validate(raw)
    # Merge flow defaults under the call's stored dynamic_vars (the call's personalization),
    # mirroring the chat flow path. Mid-call agent-side var updates are NOT reflected (deferred).
    merged_custom: dict[str, object] = {}
    flow_defaults = flow_config.get("default_dynamic_variables")
    if isinstance(flow_defaults, dict):
        merged_custom.update(flow_defaults)
    merged_custom.update(call.dynamic_vars or {})
    values = build_vars({}, merged_custom, timezone="", now=datetime.now(UTC))
    model = flow_runtime.flow_model(flow_config, cfg.llm.model)
    return flow_config, values, model


def _assemble_instruction(flow_config: dict, node: dict, values: dict[str, str]) -> str:
    global_prompt = substitute(str(flow_config.get("global_prompt") or ""), values)
    node_text = substitute(flow_runtime.node_instruction_text(node) or "", values)
    return f"{global_prompt}\n\n{node_text}".strip()


async def advance(
    db: AsyncSession,
    settings: Settings,
    call_id: uuid.UUID,
    current_node_id: str | None,
    turns: Sequence[FlowTurn],
) -> FlowAdvanceResponse:
    """Enter at start when current_node_id is null/stale; else evaluate the current node's
    edges and advance (or remain). Returns bound=False when the call is not bound to a
    runnable flow. FlowTurn's .role/.content match the duck type evaluate_transition consumes."""
    resolved = await resolve_bound_flow(db, settings, call_id)
    if resolved is None:
        return _UNBOUND
    flow_config, values, model = resolved
    if not current_node_id or flow_runtime.node_by_id(flow_config, current_node_id) is None:
        node = flow_runtime.node_by_id(flow_config, flow_config.get("start_node_id"))
    else:
        current = flow_runtime.node_by_id(flow_config, current_node_id)
        assert current is not None  # node_by_id guard above
        dest = await flow_runtime.evaluate_transition(
            current, turns, values, model=model, settings=settings
        )
        node = flow_runtime.node_by_id(flow_config, dest) if dest else current
    if node is None:
        return _UNBOUND  # defensive: start unresolved despite runnable guard
    return FlowAdvanceResponse(
        bound=True,
        node_id=node.get("id"),
        instruction=_assemble_instruction(flow_config, node, values),
        is_end=node.get("type") == "end",
    )
```

- [ ] **Step 5: Add the endpoint to `routers/runtime.py`**

In `apps/api/src/usan_api/routers/runtime.py`, add these imports beside the existing ones:

```python
from usan_api.compat import flow_runtime_voice
from usan_api.schemas.runtime import FlowAdvanceRequest, FlowAdvanceResponse
from usan_api.settings import Settings, get_settings
```

Add the endpoint after `get_agent_config`:

```python
@router.post("/flow-advance", response_model=FlowAdvanceResponse)
async def flow_advance(
    body: FlowAdvanceRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> FlowAdvanceResponse:
    """Advance a call's bound conversation flow one node given the recent turns. Worker-token
    scoped; flag-gated. Returns bound=False when the flag is off or the call is not bound to a
    runnable flow (the agent then takes the single-prompt path). Raises only if Vertex raises."""
    if not settings.flow_runtime_voice_enabled:
        return FlowAdvanceResponse(bound=False)
    return await flow_runtime_voice.advance(
        db, settings, body.call_id, body.current_node_id, body.turns
    )
```

- [ ] **Step 6: Run the endpoint tests**

Run: `cd apps/api && uv run pytest -n0 tests/test_flow_runtime_voice.py -q`
Expected: PASS (all cases).

- [ ] **Step 7: Full apps/api gate**

Run: `cd apps/api && ruff check . && ruff format --check . && uv run mypy && uv run pytest -q`
Expected: clean + green.

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/schemas/runtime.py apps/api/src/usan_api/compat/flow_runtime_voice.py apps/api/src/usan_api/routers/runtime.py apps/api/tests/test_flow_runtime_voice.py
git commit -m "feat(api): flow-advance endpoint + voice flow resolver (Phase 6-runtime-voice)"
```

---

## Task 3: Agent settings flag + `flow_advance` API client

Add the agent-side flag and the thin best-effort HTTP client for the new endpoint.

**Files:**
- Modify: `services/agent/src/usan_agent/settings.py`
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_api_client_flow_advance.py`

**Interfaces:**
- Produces (settings): `flow_runtime_voice_enabled: bool` (alias `FLOW_RUNTIME_VOICE_ENABLED`, default False).
- Produces (api_client): `async flow_advance(call_id: str, settings: Settings, *, current_node_id: str | None, turns: list[dict[str, str]]) -> dict[str, Any] | None` — parsed JSON on 200, `None` on any failure (never raises).

- [ ] **Step 1: Write failing client tests**

Create `services/agent/tests/test_api_client_flow_advance.py`:

```python
from __future__ import annotations

from typing import Any

import httpx

from usan_agent import api_client
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
    )


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


async def test_flow_advance_returns_json_on_200(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> "_Client":
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            captured["url"] = url
            captured["json"] = json
            return _Resp(200, {"bound": True, "node_id": "n1", "instruction": "Hi", "is_end": False})

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    out = await api_client.flow_advance(
        "11111111-1111-1111-1111-111111111111",
        _settings(),
        current_node_id=None,
        turns=[{"role": "user", "content": "hello"}],
    )
    assert out == {"bound": True, "node_id": "n1", "instruction": "Hi", "is_end": False}
    assert captured["url"].endswith("/v1/runtime/flow-advance")
    assert captured["json"]["turns"] == [{"role": "user", "content": "hello"}]


async def test_flow_advance_returns_none_on_error(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> "_Client":
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, *a: Any, **k: Any) -> _Resp:
            return _Resp(500, {})

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    out = await api_client.flow_advance(
        "11111111-1111-1111-1111-111111111111",
        _settings(),
        current_node_id="n1",
        turns=[],
    )
    assert out is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent && uv run pytest tests/test_api_client_flow_advance.py -q`
Expected: FAIL (`flow_advance` undefined).

- [ ] **Step 3: Add the flag to agent `settings.py`**

In `services/agent/src/usan_agent/settings.py`, immediately after the `kb_retrieval_voice_enabled` field, add:

```python
    flow_runtime_voice_enabled: bool = Field(default=False, alias="FLOW_RUNTIME_VOICE_ENABLED")
```

- [ ] **Step 4: Implement `flow_advance` in `api_client.py`**

Add to `services/agent/src/usan_agent/api_client.py` (near `retrieve_kb_context`):

```python
async def flow_advance(
    call_id: str,
    settings: Settings,
    *,
    current_node_id: str | None,
    turns: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Best-effort one-turn conversation-flow advance. Returns the parsed JSON
    ({bound, node_id, instruction, is_end}) or None on any failure — a failed advance leaves
    the caller on its current node and never breaks the turn. PHI-safe: logs only the
    exception type, never turn/instruction content."""
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/runtime/flow-advance"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        payload = {"call_id": call_id, "current_node_id": current_node_id, "turns": turns}
        async with httpx.AsyncClient(timeout=_CONFIG_TIMEOUT_S) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        return cast(dict[str, Any], body)
    except Exception as exc:
        logger.bind(call_id=call_id, err=type(exc).__name__).warning(
            "flow advance call failed; staying on current node"
        )
        return None
```

(`cast`, `httpx`, `logger`, `_mint_token`, `_validate_call_id`, `_CONFIG_TIMEOUT_S` are already imported/defined in this module.)

- [ ] **Step 5: Run the client tests**

Run: `cd services/agent && uv run pytest tests/test_api_client_flow_advance.py -q`
Expected: PASS.

- [ ] **Step 6: Lint + type**

Run: `cd services/agent && ruff check src/usan_agent/api_client.py src/usan_agent/settings.py tests/test_api_client_flow_advance.py && uv run mypy`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add services/agent/src/usan_agent/settings.py services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client_flow_advance.py
git commit -m "feat(agent): flow_advance API client + voice flow flag"
```

---

## Task 4: RagAgent per-turn flow steering

Wire the flow step into `RagAgent.on_user_turn_completed`, deriving the flag from settings (like `_kb_enabled`) and reusing the existing `call_id`/`settings` the builders already pass — so no builder/worker/pipeline changes are needed. Refactor the existing kb body into its own guarded method so the two hooks are independent.

**Files:**
- Modify: `services/agent/src/usan_agent/rag_agent.py`
- Test: `services/agent/tests/test_rag_agent_flow.py`

**Interfaces:**
- Consumes: `api_client.flow_advance`; `self._kb_call_id` (the call_id already stored); `self.update_instructions` (LiveKit `Agent`).
- Produces: `RagAgent` holds `_flow_enabled`, `_flow_node_id: str | None`, `_flow_latched_off: bool`; `on_user_turn_completed` runs the flow step then the kb step, each independently guarded; module helper `_turns_for_flow`.

- [ ] **Step 1: Write failing tests**

Create `services/agent/tests/test_rag_agent_flow.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock

from livekit.agents import llm

from usan_agent import api_client
from usan_agent.rag_agent import RagAgent
from usan_agent.settings import Settings


def _settings(*, flow: bool, kb: bool = False) -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
        FLOW_RUNTIME_VOICE_ENABLED=flow,
        KB_RETRIEVAL_VOICE_ENABLED=kb,
    )


def _agent(settings: Settings, **kw) -> RagAgent:
    return RagAgent(instructions="base", call_id="c-1", settings=settings, **kw)


def _user_msg(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


async def test_flow_off_never_calls_advance(monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(_settings(flow=False))
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("hi"))
    spy.assert_not_awaited()


async def test_bound_steers_instruction_and_advances_cursor(monkeypatch) -> None:
    spy = AsyncMock(
        return_value={"bound": True, "node_id": "n2", "instruction": "Ask about meds", "is_end": False}
    )
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    update = AsyncMock()
    agent = _agent(_settings(flow=True))
    monkeypatch.setattr(agent, "update_instructions", update)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("hello"))
    update.assert_awaited_once_with("Ask about meds")
    assert agent._flow_node_id == "n2"


async def test_unbound_latches_off_after_one_call(monkeypatch) -> None:
    spy = AsyncMock(return_value={"bound": False})
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(_settings(flow=True))
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("a"))
    await agent.on_user_turn_completed(ctx, _user_msg("b"))
    assert spy.await_count == 1  # latched off after the first bound=False


async def test_advance_failure_does_not_latch_or_raise(monkeypatch) -> None:
    spy = AsyncMock(return_value=None)  # transient failure
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    update = AsyncMock()
    agent = _agent(_settings(flow=True))
    monkeypatch.setattr(agent, "update_instructions", update)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("a"))
    await agent.on_user_turn_completed(ctx, _user_msg("b"))
    assert spy.await_count == 2  # retried; no latch
    update.assert_not_awaited()
    assert agent._flow_node_id is None


async def test_kb_hook_still_runs_alongside_flow(monkeypatch) -> None:
    monkeypatch.setattr(api_client, "flow_advance", AsyncMock(return_value={"bound": False}))
    kb = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", kb)
    agent = _agent(_settings(flow=True, kb=True), kb_ids=["kb_1"])
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("how do meds work"))
    kb.assert_awaited_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent && uv run pytest tests/test_rag_agent_flow.py -q`
Expected: FAIL (`_flow_node_id` missing / advance not wired).

- [ ] **Step 3: Implement the flow step in `rag_agent.py`**

In `services/agent/src/usan_agent/rag_agent.py`, in `__init__`, after the existing kb fields, add:

```python
        # Phase 6-runtime-voice: derive the flow flag from settings (like _kb_enabled). The
        # cursor is held here (agent owns the call); the endpoint is stateless. Once an advance
        # definitively reports the call is not flow-bound, latch off (no per-turn calls on the
        # common non-flow path). A transient failure (None) does NOT latch.
        self._flow_enabled = bool(settings and settings.flow_runtime_voice_enabled)
        self._flow_node_id: str | None = None
        self._flow_latched_off = False
```

Replace the body of `on_user_turn_completed` so it runs both steps, each independently guarded, and move the existing kb logic into `_maybe_inject_kb`:

```python
    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        await self._maybe_advance_flow(turn_ctx, new_message)
        await self._maybe_inject_kb(turn_ctx, new_message)

    async def _maybe_advance_flow(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (self._flow_enabled and self._kb_call_id) or self._flow_latched_off:
                return
            if self._kb_settings is None:
                return
            turns = _turns_for_flow(turn_ctx, new_message)
            result = await api_client.flow_advance(
                self._kb_call_id,
                self._kb_settings,
                current_node_id=self._flow_node_id,
                turns=turns,
            )
            if result is None:
                return  # transient failure: retry next turn, no latch, stay on current node
            if not result.get("bound"):
                self._flow_latched_off = True
                return
            instruction = result.get("instruction")
            if isinstance(instruction, str) and instruction:
                await self.update_instructions(instruction)
            node_id = result.get("node_id")
            if isinstance(node_id, str) and node_id:
                self._flow_node_id = node_id
        except Exception as exc:  # a failure here must never abort the turn
            logger.bind(err=type(exc).__name__).warning(
                "voice flow advance hook failed; staying on current node"
            )

    async def _maybe_inject_kb(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (
                self._kb_enabled
                and self._kb_call_id
                and self._kb_ids
                and self._kb_settings is not None
            ):
                return
            query = new_message.text_content
            if not query or not query.strip():
                return
            context = await api_client.retrieve_kb_context(
                self._kb_call_id, self._kb_settings, query
            )
            if context:
                turn_ctx.add_message(
                    role="system", content=_CONTEXT_PREFIX + context + _CONTEXT_SUFFIX
                )
        except Exception as exc:  # an exception here would abort the turn — swallow it
            logger.bind(err=type(exc).__name__).warning(
                "voice kb retrieval hook failed; continuing without context"
            )
```

Add the turn-window helper at module scope (below the imports, above the class):

```python
_FLOW_TURN_WINDOW = 20


def _turns_for_flow(
    turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
) -> list[dict[str, str]]:
    """Build the recent conversation window for the classifier. LiveKit 'assistant' turns map
    to 'agent' (so the server treats them as model turns); system turns (e.g. injected kb
    context) are excluded. The just-completed user message is appended last."""
    turns: list[dict[str, str]] = []
    for item in turn_ctx.items:
        if not isinstance(item, llm.ChatMessage):
            continue
        if item.role not in ("user", "assistant"):
            continue
        text = item.text_content
        if not text:
            continue
        turns.append({"role": "agent" if item.role == "assistant" else "user", "content": text})
    new_text = new_message.text_content
    if new_text:
        turns.append({"role": "user", "content": new_text})
    return turns[-_FLOW_TURN_WINDOW:]
```

- [ ] **Step 4: Run the flow tests**

Run: `cd services/agent && uv run pytest tests/test_rag_agent_flow.py tests/test_rag_agent.py -q`
Expected: PASS (new flow tests + the existing kb tests still green).

- [ ] **Step 5: Full agent gate**

Run: `cd services/agent && ruff check . && ruff format --check . && uv run mypy && uv run pytest -q`
Expected: clean + green.

- [ ] **Step 6: Commit**

```bash
git add services/agent/src/usan_agent/rag_agent.py services/agent/tests/test_rag_agent_flow.py
git commit -m "feat(agent): per-turn conversation-flow steering in RagAgent"
```

---

## Task 5: infra passthrough + operator doc

Ship the env passthrough (both services) and the deployment note. Inert until the flag flips.

**Files:**
- Modify: `infra/docker-compose.yml`
- Modify: `infra/.env.prod.example`
- Create: `docs/deployment/flow-runtime-voice.md`

- [ ] **Step 1: Add the compose passthrough**

In `infra/docker-compose.yml`, add `FLOW_RUNTIME_VOICE_ENABLED` to the `environment:` map of BOTH the `api` and the `agent` services, mirroring how `FLOW_RUNTIME_ENABLED` / `KB_RETRIEVAL_VOICE_ENABLED` are passed. Use the same `${FLOW_RUNTIME_VOICE_ENABLED:-false}` default form the file already uses for boolean flags. (Compose-passthrough gotcha: a new key silently no-ops unless it is in both the compose `environment:` map and the VM `.env`.)

- [ ] **Step 2: Add to the env example**

In `infra/.env.prod.example`, add near the other flow/kb flags:

```
# Conversation-flow runtime for VOICE calls (Phase 6-runtime-voice). Requires GCP_PROJECT.
FLOW_RUNTIME_VOICE_ENABLED=false
```

- [ ] **Step 3: Write the operator doc**

Create `docs/deployment/flow-runtime-voice.md`:

```markdown
# Conversation-flow runtime — voice (Phase 6-runtime-voice)

**Status:** merged, INERT until BOTH the next `v*` tag deploy AND `FLOW_RUNTIME_VOICE_ENABLED=true`
(merged ≠ deployed; flag default off). No migration, no new compat op — one new worker-facing
endpoint (`POST /v1/runtime/flow-advance`).

## What it does

When `FLOW_RUNTIME_VOICE_ENABLED` is on and a voice call resolves to an agent bound to a
**runnable** conversation flow (via the voice `create-agent` `response_engine:
{type: conversation-flow, ...}`, Phase 6c), the agent steers its active system prompt
node-by-node: each completed user turn calls `/v1/runtime/flow-advance`, which runs the shared
`flow_runtime` interpreter server-side (Vertex classifier, BAA-covered) and returns the next
node's instruction. LiveKit keeps owning STT/LLM/TTS, turn detection, barge-in, and tools.

## Runnable = what v1 executes

Only `conversation` and `end` nodes, routed by `prompt` (LLM-classified), `equation`
(deterministic), `Else`, and `Always` edges — identical to the chat runtime. Any other node
type, or a missing / archived / cross-org / malformed binding, is **not runnable**: the call
runs today's single static prompt (whole-session fallback). The runtime never breaks a live call.

## Activation

1. Seed `FLOW_RUNTIME_VOICE_ENABLED=true` into Secret Manager `usan-prod-env` AND the VM `.env`
   BEFORE the `v*` tag (deploy never re-fetches the secret). Requires `GCP_PROJECT` (Vertex) on
   the api side for the classifier.
2. Cut the tag. Bind a voice agent to a fully-supported flow and place a call.

## Deferred (not honored)

- The opening greeting still uses the agent's configured greeting; the flow engages from the
  first user turn (the start node's instruction takes effect on the agent's first post-greeting
  turn). Greeting-under-the-start-node is a follow-up.
- Mid-call agent-side dynamic-variable updates are not reflected in server-side equation
  evaluation (values are resolved from `call.dynamic_vars` + flow defaults at advance time).
- 13 of 15 node types, tool/function nodes, per-node model/voice overrides,
  `global_node_setting` global transitions, `flex_mode`, auto-hangup on an `end` node,
  `start_speaker: "user"`. Advancement is single-hop per user turn.
```

- [ ] **Step 4: Validate compose**

Run: `docker compose -f infra/docker-compose.yml config >/dev/null && echo OK`
Expected: `OK` (compose parses; the new env key resolves).

- [ ] **Step 5: Commit**

```bash
git add infra/docker-compose.yml infra/.env.prod.example docs/deployment/flow-runtime-voice.md
git commit -m "infra: FLOW_RUNTIME_VOICE_ENABLED passthrough + operator doc"
```

---

## Done criteria

- `POST /v1/runtime/flow-advance` advances a bound runnable flow, returns `bound=false` for
  unbound/unrunnable/cross-org/flag-off/missing-call, worker-token gated.
- A flow-bound voice agent (flag on) is steered node-by-node from the first user turn; a
  non-flow call pays exactly one advance call then latches off; every failure path stays on the
  current node and never aborts a turn.
- Both suites green, ruff + mypy clean, no cross-app import, single alembic head unchanged
  (no migration), feature inert until the flag flips.
