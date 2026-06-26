# Deploying Phase 4a — Chat (api_chat) sessions

Phase 4a serves 7 RetellAI `api_chat` operations from the compat sub-app. It is **inert**
until deployed.

## Operator checklist

1. **Migration 0042** (`chat_sessions`, `chat_messages`, the `chat_status` enum) is owner-DDL —
   it runs as the `usan` table owner before `compose up` (same path as 0041). Verify
   `alembic heads` is a single `0042` after deploy.
2. **`GCP_PROJECT`** must be set in the API service env for `create-chat-completion` to run the
   live Vertex turn. Without it the endpoint returns **503** ("chat completion unavailable");
   the other six ops (create/get/list/update/end/delete) work without it. Vertex auth is ADC
   (the attached VM service account) — never the Gemini Developer API.
3. **No compat master flag.** Every chat op returns **401** until a super-admin mints a compat
   key (`/compat-keys`).

## Deviations from RetellAI (4a)

- `chat_type` is always `api_chat` (SMS → 4b).
- No `chat_analysis` / `collected_dynamic_variables` / `chat_cost` / `custom_attributes` in
  responses (no post-chat analysis yet); `rerun-chat-analysis` returns 501.
- Agent replies are `role=agent` text only (no tool-call/transition message roles).
- `/v3/list-chats` rich filters (sentiment, success, cost/duration ranges, custom fields) are
  accepted-but-not-honored; only `agent` + `chat_status` filter. `data_storage_setting` on
  update-chat is accepted-and-ignored.
- `create-chat-completion` is synchronous (one Vertex turn); the reply is one completed agent
  message (shape-conformant).
