# Conversation Flows (RetellAI parity, Phase 6a)

## What this is
The 5 conversation-flow CRUD ops (`create/get/update/delete-conversation-flow`,
`GET /v2/list-conversation-flows`) are served at the RetellAI oracle paths and shapes. A flow's
DAG is **persisted and echoed conformantly but NOT executed** at call/chat time
(persisted-not-honored). The DAG runtime, the 5 conversation-flow-**component** ops, and the
agent->flow binding are later sub-phases (6b / 6c / 6-runtime).

## Activation
Nothing to flip. The compat surface is gated only by a minted compat key; the new routes are
inert (a stored flow is never run). Migration `0048` (the `conversation_flows` table, FORCE-RLS)
ships with the next `v*` tag and is owner-DDL (runs as the `usan` owner via the deploy migration
path). No new env keys, no feature flag.

## Posture (documented deviations)
- **Accept-and-echo:** only `start_speaker`, `model_choice`, `nodes` are presence-checked on
  create (422 if absent). Node graphs, `model_choice.model` (RetellAI's OpenAI/Anthropic/Gemini
  enum - which we do not run), and edges are stored opaquely and never validated or interpreted.
- **Current-only versioning:** `version` starts at 0 and increments on each update;
  `?version` on get/update is accepted but always serves current (no version history).
- **Soft delete:** delete sets `archived_at`; get/list exclude archived rows.
- **RLS:** `conversation_flows` is per-org FORCE-RLS; isolation is enforced for the
  least-privilege `usan_app` runtime role.

## Security / PHI
Flow `config` may carry prompts -> it is NEVER logged (audit logs carry org + op only). The
RetellAI error envelope (`{"status": <int>, "message": ...}`) never echoes request bodies.

## Known limitation
Single-org persisted-not-honored CRUD. Binding a flow to an agent (`response_engine.type=
"conversation-flow"`) and executing it are deferred.
