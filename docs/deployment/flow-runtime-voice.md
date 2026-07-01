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
