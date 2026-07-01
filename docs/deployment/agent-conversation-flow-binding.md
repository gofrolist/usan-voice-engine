# Agent ↔ Conversation-Flow binding (Phase 6c)

**Status:** merged, INERT until the next `v*` tag deploy (merged ≠ deployed). No new env
keys, no migration (single alembic head stays `0049`).

## What shipped

`create-agent` / `update-agent` now accept the oracle `response_engine` variant
`{type: "conversation-flow", conversation_flow_id, version?}` (in addition to `retell-llm`).
`get-agent` / `list-agents` / `get-agent-versions` echo the bound variant.

The binding is stored as a `compat_response_engine` key in the agent profile's JSONB config
(no schema change) and validated against the org's own `conversation_flows` rows (Phase 6a,
RLS-scoped). A cross-org / unknown / malformed flow id returns `422 "unknown
conversation_flow_id"` (never acknowledging cross-org existence).

## Persisted-not-honored (IMPORTANT)

The bound conversation flow is **echoed but NOT executed.** A conversation-flow agent runs
the engine's default Vertex pipeline against its (empty/default) prompt config — it does
**not** run the flow DAG. Honoring the DAG at call time is the later **6-runtime** phase.
Calls to a flow agent are **not blocked** (accept-and-persist posture, matching phone-number
bindings and `current_node_id`). Do not point production traffic at a flow-only agent
expecting flow behavior until 6-runtime ships.

## Not covered

- `custom-llm` engines are rejected (`422 "unsupported response_engine type"`) — we never
  dial an external LLM websocket (PHI containment).
- **Chat-agent** flow binding (`chat_agent_bridge`) is a deferred **6c-chat** follow-up.
- `update-agent` reverting a flow agent to `retell-llm` requires the agent's OWN `llm_id`
  (self); a foreign `llm_id` returns `409` (one-profile overlay can't share an llm).
