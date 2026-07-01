# Phase 6-runtime-voice — Conversation-flow runtime for voice calls

**Status:** design approved 2026-07-01. Implements the voice half deferred by
`2026-07-01-retell-parity-phase6-runtime-chat-design.md`.

## Goal

Execute a bound RetellAI conversation-flow DAG turn-by-turn on a live voice call, so a
flow-bound voice agent is steered by its flow instead of running the single static
check-in prompt. Flag-gated (`FLOW_RUNTIME_VOICE_ENABLED`), default off, whole-session
fallback — any unsupported node/edge or resolution failure runs today's single-prompt
path and never breaks a live call.

## Non-goals (deferred)

- 13 of 15 node types (`function`, `transfer_call`, `branch`, `sms`,
  `extract_dynamic_variables`, `agent_swap`, `mcp`, `subagent`, `code`, `press_digit`,
  `bridge_transfer`, `cancel_transfer`, `component`). v1 executes only `conversation` and
  `end`, same runnable contract as 6-runtime-chat.
- Per-node model/voice overrides, tool-node execution, `global_node_setting` global
  transitions, `flex_mode`.
- Auto-hangup on an `end` node (the call ends by the existing call-lifecycle path;
  `is_end` is informational for v1).
- `start_speaker: "user"` — the start node always speaks first.
- Multi-hop advancement per turn (one transition evaluation per completed user turn).
- Reflecting mid-call agent-side dynamic-var updates in server-side equation evaluation
  (values are resolved from the call/contact at advance time).

## Architecture

The flow **steers the active system prompt**; LiveKit continues to own STT, LLM, TTS,
turn detection, barge-in, and tools. Each `conversation` node maps to a system prompt.

The DAG interpreter is **not duplicated**. `apps/api/.../compat/flow_runtime.py` (built in
6-runtime-chat) stays the single implementation. `services/agent` — which cannot import
`apps/api` (Constitution I) and has no raw-Vertex path — reaches the interpreter only over
HTTP through one new endpoint. This is the first program phase that legitimately spans both
apps; it does so with **no cross-app import**.

Cursor state (the current node id) is **held by the agent**, which owns the call lifecycle.
The advance endpoint is stateless with respect to the cursor, so voice needs **no migration**
(unlike chat's `chat_sessions.flow_current_node_id`).

## Components

### apps/api

**`compat/flow_runtime_voice.py`** — a call-oriented resolver reusing `flow_runtime`
(`flow_is_runnable`, `node_by_id`, `evaluate_transition`, `_instruction_text` assembly).
It has no `speak()` — voice never generates speech server-side.

- `async resolve_bound_flow(db, settings, call_id) -> (flow_config, values) | None`
  Resolves the call → its published agent version (same resolution the runtime
  agent-config endpoint uses), reads the **raw** `version.config`, decodes
  `compat_response_engine.conversation_flow_id` (catching `CompatError` → `None`), loads
  the org-scoped `conversation_flow` (RLS; cross-org id is indistinguishable from absent →
  `None`), and returns the flow config plus the call's server-resolved dynamic-var
  `values`. Returns `None` when not flow-bound, binding malformed, flow missing, or
  `flow_is_runnable(config)` is `False`.
- `async advance(db, settings, call_id, current_node_id, turns) -> AdvanceResult`
  Calls `resolve_bound_flow`; on `None` returns `bound=False`. Else: if `current_node_id`
  is null or not present in the resolved flow (stale/repoint) → enter at `start_node_id`;
  otherwise `evaluate_transition(current_node, turns, values, ...)` and, if it returns a
  destination, move there (else remain on the current node). Assembles
  `instruction = substitute(global_prompt) + "\n\n" + substitute(node instruction)` for the
  resulting node and returns `bound=True, node_id, instruction, is_end`.

`turns` is a bounded window of `{role, content}` sent by the agent. Roles map through
`flow_runtime.history_to_contents` (a lightweight adapter shapes the request items into the
`.role`/`.content` duck type that helper already consumes).

**`POST /v1/runtime/flow-advance`** in `routers/runtime.py` — worker-token scoped, gated by
the api-side `flow_runtime_voice_enabled`. When the flag is off it returns `bound=False`
(the agent then always takes the normal path). Request/response schemas live in
`schemas/runtime.py` (or inline in the router, matching the file's existing style):

```
FlowAdvanceRequest:  { call_id: UUID, current_node_id: str | None, turns: [FlowTurn] }
FlowTurn:            { role: str, content: str }
FlowAdvanceResponse: { bound: bool, node_id: str | None, instruction: str | None,
                       is_end: bool }
```

The endpoint delegates to `flow_runtime_voice.advance`. It never raises for flow-shape
reasons (a malformed/absent binding is `bound=False`, not an error); it raises only if
Vertex itself raises, exactly as the chat `generate_agent_reply` path does.

**`settings.py`** — add `flow_runtime_voice_enabled: bool = Field(default=False,
alias="FLOW_RUNTIME_VOICE_ENABLED")`. Separate from chat's `flow_runtime_enabled` so voice
and chat enable independently.

### services/agent

**`settings.py`** — add `flow_runtime_voice_enabled: bool` (alias
`FLOW_RUNTIME_VOICE_ENABLED`, default `False`) so the agent gates its hook.

**`api_client.py`** — `async flow_advance(call_id, settings, *, current_node_id, turns) ->
dict[str, Any] | None`. Mints the per-call token, POSTs to `/v1/runtime/flow-advance` with a
tight timeout (reuse the config/kb timeout tier), returns the parsed JSON or `None` on any
failure. PHI-safe: logs only `type(exc).__name__` — never turn content.

**`rag_agent.py`** — `RagAgent` gains flow state and a flow step in
`on_user_turn_completed`, mirroring the existing kb-retrieval hook (gated,
exception-guarded, best-effort):

- constructor: `flow_enabled: bool` (derived from `settings.flow_runtime_voice_enabled`),
  `flow_node_id: str | None` (the start cursor when flow-active; `None` otherwise).
- a `flow_active` property (`flow_enabled and flow_node_id is not None`).
- in `on_user_turn_completed`: if flow-active, build the recent-turn window from
  `turn_ctx`, call `api_client.flow_advance(cursor, turns)`; on a `bound=True` result,
  `await self.update_instructions(result["instruction"])` and store `result["node_id"]` as
  the new cursor. Any failure/`None`/`bound=False` is swallowed → the turn proceeds under
  the current node's instruction (stay put; never abort the turn). The kb-retrieval step is
  unchanged and independent.

**`pipeline.py`** — `build_agent` accepts the flow params and threads them into `RagAgent`.

**`worker.py`** — a small helper (`maybe_begin_flow`) called once after the config fetch on
the conversational session-start paths: when the agent flag is on, it calls
`flow_advance(current_node_id=None, turns=[])`. Flag off → skipped (normal path unchanged).
`bound=False` → normal path. `bound=True` → the agent is built flow-active at the returned
start node, its instructions set to the start node's `instruction`, and the opening greeting
is driven under that instruction. The recording disclosure still plays first (spec §10
compliance is unchanged). The helper keeps the wiring in one place so the several
session-start sites are not each edited ad hoc.

### infra

`FLOW_RUNTIME_VOICE_ENABLED` passthrough in `infra/docker-compose.yml` for **both** the
`api` and `agent` services, and in `infra/.env.prod.example` (the compose-passthrough
gotcha: a new key silently no-ops unless it is in both the compose `environment:` map and
the VM `.env`). Requires `GCP_PROJECT` (Vertex) on the api side for the classifier.

## Data flow (per turn)

```
user speaks → STT → on_user_turn_completed
  → agent POSTs {call_id, cursor, recent turns} to /v1/runtime/flow-advance
  → API resolves bound flow (raw version.config, org-scoped, RLS), resolves values,
    classifies the transition via Vertex (BAA-covered), returns {node_id, instruction}
  → agent update_instructions(instruction) + stores node_id
  → LiveKit LLM generates the reply under the new prompt → TTS speaks
```

## Error handling & PHI

- Every new agent path is exception-guarded and best-effort, identical to the kb hook: an
  advance failure leaves the agent on the current node. Endpoint failures never crash a
  call (the agent swallows them).
- Logs carry only `type(exc).__name__` and counts — never turn/instruction content.
- Cross-org and malformed bindings are indistinguishable from "no flow" (`bound=False`).
- Sending turn text to our own API over the JWT-authed channel is the same trust boundary
  as the existing `flush_transcript`; classification runs on Vertex (BAA-covered) only —
  never a non-covered LLM.

## Reachability

The flow binding is created via the **voice** `create-agent` / `update-agent` path
(`response_engine: {type: conversation-flow, ...}`, Phase 6c) — so a voice agent **can** be
flow-bound, and this runtime is reachable through the supported voice-agent APIs (unlike the
chat runtime, whose binding path is the deferred 6c-chat). An outbound or inbound call that
resolves to a flow-bound published agent version, with the flag on, is steered by the flow.

## Testing

**apps/api** (`tests/`):
- `flow_runtime_voice.advance` / endpoint: bound flow advances start→next; not-bound →
  `bound=False`; unrunnable flow → `bound=False`; stale/absent `current_node_id` → re-enter
  at start; `end` node → `is_end=True`; **cross-org flow id → `bound=False`** (RLS
  isolation, indistinguishable from absent); flag off → `bound=False`.

**services/agent** (`tests/`):
- `RagAgent` flow hook (mock `api_client.flow_advance`): flag off → no advance call; not
  flow-active → no steering; `bound=True` → `update_instructions` called with the returned
  instruction and cursor advances; advance failure/`None` → swallowed, cursor unchanged, no
  raise; kb-retrieval hook still runs independently.
- `flow_advance` client: returns parsed dict on 200; returns `None` (no raise) on error.

## File structure

- apps/api create: `src/usan_api/compat/flow_runtime_voice.py`,
  `tests/.../test_flow_runtime_voice.py` (+ endpoint tests)
- apps/api modify: `src/usan_api/routers/runtime.py`,
  `src/usan_api/schemas/runtime.py` (new or extended), `src/usan_api/settings.py`
- agent create: `tests/.../test_rag_agent_flow.py`
- agent modify: `src/usan_agent/settings.py`, `src/usan_agent/api_client.py`,
  `src/usan_agent/rag_agent.py`, `src/usan_agent/pipeline.py`, `src/usan_agent/worker.py`
- infra modify: `infra/docker-compose.yml`, `infra/.env.prod.example`
- docs create: `docs/deployment/flow-runtime-voice.md`
