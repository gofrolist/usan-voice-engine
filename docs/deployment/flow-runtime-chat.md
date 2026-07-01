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
