# agent-playground-completion (Phase 7 slice 1)

`POST /agent-playground-completion/{agent_id}` — a stateless, single-turn Vertex
completion. Takes a `messages` history + optional `dynamic_variables`; returns one
agent message. RetellAI clients calling `retell.playground.completion(...)` reach it
with zero changes.

## Gates (ships inert)

- **Compat-key auth** — the whole compat surface 401s until a super-admin mints a key.
- **`GCP_PROJECT`** — 503 ("playground completion unavailable") until set; the Vertex
  turn runs via ADC (`vertexai=True`), never the Gemini Developer API (PHI containment).

No migration, no new env key. Not live until a `v*` tag deploy.

## Accepted-and-ignored (POSTURE)

| Field | Handling |
|-------|----------|
| `?version` | ignored — currently-published config served |
| `tool_mocks` | ignored — single `tools=[]` turn |
| `current_state` / `current_node_id` / `component_id` | ignored — no state/flow execution |
| response `current_state` / `current_node_id` / `dynamic_variables` / `call_ended` / `knowledge_base_retrieved_contents` | omitted (`exclude_none`) |

Unknown / cross-org / unpublished agent → **422** "agent is not available" (this op
declares no 404). Nothing is persisted or sent; only `type(exc).__name__` is logged on
failure.
