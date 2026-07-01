# Conversation-Flow Components (RetellAI-compat, Phase 6b)

Five endpoints — `create` / `get` / `update` (PATCH) / `delete` / `v2/list` — for **shared
conversation-flow components**, on their own `conversation_flow_components` table (migration 0049,
FORCE row-level-security, per-org isolated).

## Status: persisted-not-honored

The component body (`name`, `nodes`, `flex_mode`, `tools`, `mcps`, …) is stored as opaque JSONB and
echoed back conformantly. It is **not executed** at call/chat time — the DAG runtime is a later
sub-phase. Only the 2 oracle-required create fields (`name`, `nodes`) are validated; everything
else rides through verbatim.

## Delete semantics

The RetellAI oracle documents delete as "creates local copies for all linked conversation flows."
We **do not back** that fan-out — nothing links components to flows at rest (components ride inside
flow `config` as opaque JSON, and there is no runtime linking layer yet). `DELETE` performs a plain
soft-delete (sets `archived_at`) and returns 204. When the DAG runtime lands, revisit this.

## Server-owned fields

`conversation_flow_component_id` and `user_modified_timestamp` are always derived from the row and
stripped from stored config — a client cannot spoof them. Note: unlike conversation flows,
components have **no `version`** field (the oracle response omits it).

## Deploy

Merged ≠ deployed. This surface is inert until the next `v*` tag deploy runs migration 0049 and
ships the new router. It requires no new env keys.
