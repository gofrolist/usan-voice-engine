# RetellAI Parity Phase 6-runtime-chat — Conversation-Flow DAG runtime (chat/SMS) — design

**Date:** 2026-07-01 · **Type:** Phase design spec · **Status:** Approved — ready for implementation plan

> Phases 6a (flow CRUD, #150), 6b (flow-component CRUD, #151), and 6c (agent↔flow
> binding, #152) let a client author conversation flows and bind an agent to one — but the
> flow is **persisted-not-honored**: at chat/SMS time the agent still runs its single
> system-prompt Vertex turn (`generate_agent_reply`), ignoring the bound DAG. **6-runtime-chat**
> is the first slice that actually *executes* the flow: a turn-by-turn DAG interpreter behind
> the single `generate_agent_reply` chokepoint, gated by a default-off flag, with a
> whole-session fallback to today's behavior for anything the v1 interpreter can't run.

## 1. Goal & scope

When `flow_runtime_enabled` is **on** AND a chat/SMS agent is bound to a **runnable**
conversation flow, `generate_agent_reply` executes the flow DAG turn-by-turn instead of the
single-prompt Vertex call. Merged-inert (flag default off). Every unbound agent, every
non-runnable flow, and the entire flag-off world behaves **exactly as today**.

**In scope (chat/SMS only):**
- A DAG interpreter (`compat/flow_runtime.py`) that traverses `conversation` and `end` nodes,
  routing on `prompt` / `equation` / `Else` / `Always` edges.
- Per-session cursor state (`chat_sessions.flow_current_node_id`, migration 0050).
- A flow-bound branch inside `generate_agent_reply` — inherited for free by all three call
  sites: `create_chat_completion` (api_chat), `sms_reply` (inbound SMS), `inbound_autocreate`.
- A `flow_runtime_enabled` settings flag + compose/.env passthrough (ship inert).

**Non-goals / deferred (documented, never faked):**
- **The entire voice slice** — `6-runtime-voice`: the 4 `services/agent` `build_*_agent` sites
  + the `RagAgent.on_user_turn_completed` per-turn seam + the LiveKit turn loop. Separate
  stacked PR once the interpreter core is proven in chat.
- **13 of 15 node types** — `function`, `transfer_call`, `branch`, `sms`,
  `extract_dynamic_variables`, `agent_swap`, `mcp`, `subagent`, `code`, `press_digit`,
  `bridge_transfer`, `cancel_transfer`, `component`. A flow using ANY of these is
  **not runnable** → whole-session fallback.
- **Variable extraction** — no `extract_dynamic_variables`, so the only variables available to
  `equation` edges and `substitute` are the flow's `default_dynamic_variables` + the session's
  create-time dynamic vars. Equation edges referencing undefined vars simply don't match
  (fall to Else) — correct, not faked.
- **Tools / function-calling inside the flow**, `global_node_setting` interrupt-driven global
  transitions, `flex_mode`, `is_transfer_llm`, per-node `model_choice`/`AgentOverrideConfig`
  overrides beyond the flow-level model. Accepted-and-ignored; a flow that merely *carries*
  these flow-level fields is still runnable (they're read-through, not honored), but a *node*
  that requires them is out (covered by the node-type gate).
- **No migration beyond 0050.** No new compat op, no endpoint. `KNOWN_GAPS` stays `frozenset()`.

## 2. No conformance surface (this phase is different)

Every prior parity phase (1a–6c) hit an **oracle response shape** validated by
`assert_conforms(payload, "<Component>")` + `assert_sdk_roundtrip`. **6-runtime-chat has none.**
Flow execution is *internal behavior* — the oracle defines the flow *schema* (what a client
POSTs) but no "the flow ran correctly" response component. The chat responses
(`ChatResponse` / `V3ChatResponse` / `Message`) are unchanged in shape — the reply is still
just text — so their existing frozen conformance tests keep passing untouched.

**Correctness discipline here = interpreter behavior, not shape conformance:**
- DB-free unit tests on the pure traversal core (`flow_is_runnable`, `evaluate_transition`,
  cursor advance) with the Vertex classifier/speaker mocked.
- End-to-end chat tests (mounted compat app + real PG) that create a flow, bind an agent,
  enable the flag, and assert the cursor advances and replies come from node instructions.
- A flag-off regression test asserting `generate_agent_reply` is byte-for-byte today's path.

## 3. Oracle ground truth (the DAG shape we execute)

From the pinned `apps/api/tests/compat/oracle/openapi-final.yaml`:

- **Flow body** `ConversationFlow`: `start_node_id: str` (entry), `nodes: [ConversationFlowNode]`
  (flat list, each keyed by `id`), `global_prompt: str`, `model_choice`, `start_speaker`
  (enum user|agent), `default_dynamic_variables: {str:str}`, plus deferred fields
  (`tools`, `components`, `mcps`, `flex_mode`, `is_transfer_llm`, `knowledge_base_ids`).
- **Node union** `ConversationFlowNode` = oneOf of 15 types, discriminated by `type`. v1 executes
  only `ConversationNode` (`type:"conversation"`) and `EndNode` (`type:"end"`).
  - `NodeBaseCommon`: required `id: str`; optional `name`, `display_position`, `global_node_setting`.
  - `ConversationNode`: required `instruction: NodeInstruction`; optional `edges: [NodeEdge]`,
    `else_edge`, `skip_response_edge`, `always_edge`, `knowledge_base_ids`, `AgentOverrideConfig`.
  - `EndNode`: required `type:"end"`; optional `instruction`, `speak_during_execution`. Terminal,
    no edges.
  - `NodeInstruction` = oneOf(`{type:"prompt", text}`, `{type:"static_text", text}`). Both carry
    `text`; v1 substitutes vars into `text` and uses it as the node's system instruction
    (static_text is used verbatim as the model instruction — v1 does not distinguish "speak this
    literally" vs "prompt the model with this"; a static_text node is honored as a prompt whose
    text is the literal string; documented deviation).
- **Edges** `NodeEdge`: required `id: str`, `transition_condition` = oneOf(`PromptCondition`,
  `EquationCondition`); optional `destination_node_id: str`.
  - `PromptCondition`: `{type:"prompt", prompt: str}` — LLM-evaluated natural-language condition.
  - `EquationCondition`: `{type:"equation", equations:[{left, operator, right}], operator: "||"|"&&"}`
    — deterministically evaluable. `operator ∈ {== != > >= < <= contains}`.
  - `ElseEdge` = a `PromptCondition` with `prompt == "Else"` (fallthrough).
  - `AlwaysEdge` = a `PromptCondition` with `prompt == "Always"` (unconditional).

**Interpreter view:** entry = `config["start_node_id"]`; `config["nodes"]` is a flat list
keyed by `node["id"]`; each node's `edges[]` carry `{id, transition_condition, destination_node_id}`.
Traverse: at the current node, evaluate its edges to pick the next `destination_node_id`, move
there, speak. `end` nodes terminate the walk.

## 4. State model — migration 0050

One nullable column on the existing chat table:

```sql
ALTER TABLE chat_sessions ADD COLUMN flow_current_node_id TEXT;
```

- **1:1 with the session** — the cursor into the flow DAG. `NULL` = not yet entered (first turn
  starts at `start_node_id`) OR flag-off / unbound / non-runnable (never written).
- **RLS-inherited** — `chat_sessions` is already `TenantScoped` + FORCE-RLS; the column rides
  that. Owner-DDL migration (runs as the `usan` owner per the deploy path).
- **Never echoed** — `serialize_chat` has a fixed field set and does not read this column; the
  cursor is internal-only, so no leak surface (unlike a JSONB blob that could ride into
  `metadata`). Additive + nullable ⇒ inert until the flag is flipped.
- **Why a column, not a table:** the cursor is a single string, per-session, read/written only by
  the runtime. There is no cross-org accessor to leak through and no fan-out reader. A dedicated
  table would add ceremony with no isolation benefit here.

## 5. Binding discovery — the one real gotcha

`generate_agent_reply` today calls `_load_published_config(session.agent_profile_id)` →
`AgentConfig.model_validate(version.config)`. **`AgentConfig` is `extra="ignore"`**, so it
**strips the top-level `compat_response_engine` key** that Phase 6c writes. Therefore the runtime
**cannot** discover the flow binding from the parsed `AgentConfig` — it must read the **raw**
published `version.config` dict.

Discovery path (inside the flow-bound branch, before falling through to the default turn):
1. Load the published agent version (raw): `version = agent_profiles_repo.get_published_version(...)`
   (or equivalent already used by `_load_published_config`); keep both `version.config` (raw dict)
   and the parsed `AgentConfig`.
2. `engine = (version.config or {}).get("compat_response_engine")`.
3. If `engine` is a dict with `type == "conversation-flow"` and a `conversation_flow_id`:
   `flow_uuid = ids.decode_conversation_flow_id(engine["conversation_flow_id"])` →
   `flow_row = conversation_flows_repo.get(db, flow_uuid)` (RLS-scoped; `None` if archived /
   cross-org / missing).
4. If `flow_row` is `None` OR `not flow_is_runnable(flow_row.config)` → **fall back** to today's
   single-prompt path (whole-session fallback). Otherwise → run the interpreter (§6).

A malformed / undecodable `conversation_flow_id`, a `None` flow row, or a non-dict engine all
collapse to the fallback — the runtime never raises on a bad binding (a live chat must not break
because a flow was archived out from under it).

## 6. Interpreter — `compat/flow_runtime.py`

New module, kept pure/DB-free except where it must run a Vertex turn (which it does via injected
callables/`run_vertex_turn`, so the core is unit-testable with mocks).

### 6.1 `flow_is_runnable(config: dict) -> bool` — the whole-session gate

Returns `True` iff **all** hold:
- `config.get("start_node_id")` is a non-empty str that matches some `node["id"]` in
  `config.get("nodes", [])`.
- every node's `type` ∈ `{"conversation", "end"}`.
- every conversation node has an `instruction` dict with a readable `text`; an `end` node may
  have no instruction (silent) or an instruction dict with `text`.
- every edge's `transition_condition.type` ∈ `{"prompt", "equation"}` (Else/Always are `prompt`).
- `nodes` is non-empty and every `destination_node_id` referenced by an edge resolves to a real
  node id (a dangling destination ⇒ not runnable — we never jump into the void).

Any miss ⇒ `False` ⇒ caller uses the default single-prompt path. Cheap to compute per turn
(flows are small); no caching in v1.

### 6.2 `evaluate_transition(node, history, values, settings) -> str | None`

Pick the next `destination_node_id` from `node["edges"]`:
1. **Always** edge (`transition_condition.prompt == "Always"`) → return its `destination_node_id`
   immediately (first one wins).
2. **Equation** edges → evaluate `equations` against `values` with the edge's boolean `operator`
   (`"||"`/`"&&"`); a missing var makes its `Equation` false. First satisfied edge → its
   `destination_node_id`.
3. **Prompt** edges (excluding Else) + the **Else** edge → **one** Vertex classification turn:
   system instruction lists the numbered prompt conditions; contents = the conversation history;
   the model replies with the matching index or `none`. Map the index → that edge's
   `destination_node_id`; `none` (or an unparseable reply) → the Else edge's destination if present,
   else `None`.
4. No edge matched and no Else → return `None` (caller: **remain** on the current node).

The equation evaluator supports `== != > >= < <= contains` over string/number operands (numeric
compare when both sides parse as numbers, else string compare; `contains` = substring). It is
deterministic and fully unit-tested; it never calls Vertex.

### 6.3 `speak(flow_config, node, values, history, settings) -> str`

- `instruction_text = substitute(node["instruction"]["text"], values)` (empty string if the node
  has no instruction — an `end` node may be silent).
- `system_instruction = substitute(flow_config.get("global_prompt", ""), values)` +
  `"\n\n"` + `instruction_text` (global prompt first, node instruction second).
- `contents` = the existing role-mapped history builder from `chat_service` (`agent→"model"`,
  else `"user"`).
- `run_vertex_turn(model=<flow model_choice or agent llm.model>, temperature=..., system_instruction,
  tools=[], contents, settings)` → `turn.text`.
- Model selection: prefer `flow_config["model_choice"]["model"]` when present and non-empty, else
  the agent's `cfg.llm.model` (the flow's model governs its own execution; documented).

### 6.4 Turn algorithm (the flow-bound branch of `generate_agent_reply`)

```
cursor = session.flow_current_node_id
values = build_vars({}, dynamic_vars_merged, timezone="", now=utcnow())   # flow default vars + session vars
history = chats_repo.list_messages(session)

if cursor is None:
    node = node_by_id(flow_config, flow_config["start_node_id"])          # ENTER — no transition eval
else:
    node = node_by_id(flow_config, cursor)
    dest = evaluate_transition(node, history, values, settings)
    if dest is not None:
        node = node_by_id(flow_config, dest)                             # ADVANCE
    # else: remain on the current node (re-speak)

reply = speak(flow_config, node, values, history, settings)
session.flow_current_node_id = node["id"]                                # persist cursor
return reply
```

- An `end` node is `speak`-ed (its optional closing instruction) and the cursor is left on it; the
  chat session is **not** auto-ended (ending stays the client's `end-chat` op — no divergence from
  the accept-and-persist posture). On a subsequent turn at an `end` node, it has no edges →
  `evaluate_transition` returns `None` → remain → re-speak. Benign.
- The system-prompt timezone stays `""` (as in today's `generate_agent_reply` — the reply is model
  output, not recipient-facing template text, so `{{current_time}}` never leaks literally; this
  matches the 4b-2 rationale, NOT the 4b-1 recipient-facing-greeting exception).

## 7. Activation

- `flow_runtime_enabled: bool = False` on the Pydantic `Settings` (validated at startup).
- Threaded into `infra/docker-compose.yml` api `environment:` map **and** `.env.prod.example`
  (ship inert) — per the compose-passthrough gotcha, a new key silently no-ops on the VM unless
  it is in BOTH the compose environment map AND the VM `.env`.
- Flag **off** ⇒ `generate_agent_reply` skips the entire flow branch and runs today's code path
  unchanged (regression-tested). No migration data is read, no cursor written.

## 8. Error & fallback policy

- **Whole-session fallback** is the safety net: unbound agent, missing/archived/cross-org flow,
  malformed binding, or `not flow_is_runnable` ⇒ today's single-prompt turn. No raise.
- A Vertex failure inside `speak`/`evaluate_transition` propagates exactly as today's
  `generate_agent_reply` Vertex failure does (the caller's existing rollback/503/502 handling in
  `create_chat_completion` / `sms_reply` / `inbound_autocreate` is unchanged — the flow branch
  returns a string or raises, same contract as the function it replaces).
- The cursor is persisted on the **same** `db` session/txn as the agent-turn message; if the
  caller rolls back on a downstream failure, the cursor advance rolls back with it (consistent —
  no orphaned advance without a persisted reply).

## 9. Tests

- `tests/compat/test_flow_runtime_unit.py` — DB-free: `flow_is_runnable` (accept a 2-node
  conversation→end flow; reject function-node, dangling-destination, missing-start; accept
  equation+prompt edge mixes; reject custom node types); the equation evaluator
  (each operator, missing-var=false, numeric vs string, `&&`/`||`); `evaluate_transition`
  (Always short-circuit, equation match, prompt-classifier via a mocked Vertex, Else fallback,
  no-match=None); `speak` prompt assembly (global_prompt + node instruction, var substitution)
  with a mocked `run_vertex_turn`.
- `tests/compat/test_flow_runtime_chat.py` — end-to-end (mounted compat app + real PG):
  create-conversation-flow (2 nodes) → create-agent bound to it → flag on → create-chat →
  create-chat-completion advances the cursor and replies from node instructions; a
  non-runnable flow (contains a function node) falls back to single-prompt; flag-off runs the
  default path; an archived flow falls back. Vertex mocked at the module boundary the runtime
  imports.
- Existing frozen conformance suites (`test_freeze_chats` etc.) must stay green untouched —
  proof the response shapes did not move.

## 10. Operator note

`docs/deployment/flow-runtime-chat.md` — records: chat/SMS flow execution is **live only behind
`flow_runtime_enabled`** (default off), honors `conversation`+`end` nodes with
prompt/equation/Else/Always edges, whole-session-fallback for anything else, the voice slice is
the deferred `6-runtime-voice` follow-up, migration 0050 adds an inert nullable column, no new
compat op, inert until the next `v*` tag (merged ≠ deployed).

## 11. Foundational-principle compliance

- **§2.2 Capability-bounded:** unsupported node types are not faked — they trigger whole-session
  fallback to the honest single-prompt path; the deferral is documented.
- **§2.4 Auth + RLS:** the flow load is RLS-scoped (`conversation_flows_repo.get`); a cross-org
  flow id is indistinguishable from absent (falls back, never acknowledged). The cursor column
  inherits `chat_sessions` FORCE-RLS.
- **§2.6 Error envelope:** the runtime never introduces a new error path — it returns a reply
  string or raises exactly as the `generate_agent_reply` it wraps, preserving each caller's
  existing envelope.
- **Merged ≠ deployed:** flag-gated + additive-inert migration; zero behavior change until the
  flag is deliberately enabled on a fully-supported flow.
