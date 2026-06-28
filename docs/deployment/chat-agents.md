# chat-agent CRUD (Phase 4c-1) — operator note

RetellAI-compatible chat-agent management: `POST /create-chat-agent`,
`GET /get-chat-agent/{agent_id}`, `GET /get-chat-agent-versions/{agent_id}`,
`GET /list-chat-agents`, `PATCH /update-chat-agent/{agent_id}`,
`DELETE /delete-chat-agent/{agent_id}`, `POST /publish-chat-agent/{agent_id}`.

A chat agent is an `agent_profiles` row with `channel='chat'` (the same overlay as a voice
agent). It is created by the two-step flow `create-retell-llm` (holds the prompt) →
`create-chat-agent` (binds `response_engine.llm_id` + chat config, marks channel='chat').

## Enable
1. Apply migration `0045` (adds `agent_profiles.channel TEXT NOT NULL DEFAULT 'voice'`).
   Owner-DDL — the deploy migrates as the `usan` owner; the new column inherits the table's
   `usan_app` GRANT + RLS policy.
2. Mint a compat key (super-admin UI) — all compat ops 401 until a key exists.

No new env keys. Inert until a `v*` tag deploys migration 0045.

## Behavior / posture
- Only `response_engine.type='retell-llm'` is honored (`custom-llm`/`conversation-flow` → 422).
- Chat config (`auto_close_message`, `end_chat_after_silence_ms`, `post_chat_analysis_data`,
  `post_chat_analysis_model`, `pii_config`, `guardrail_config`, `handbook_config`,
  `data_storage_*`, `webhook_*`, `language`, `timezone`, `version_title`) is echoed verbatim and
  **persisted-not-honored** (the analysis config is consumed by Phase 4c-2's rerun-chat-analysis).
- `version` query (`AgentVersionReference`) is accepted; the current published view is returned.
  `base_version`/`assigned_tags` are omitted; `is_latest`/`pagination_key_version` accepted-and-ignored.
- Writes always publish; delete = archive; publish = thin 200-no-body (deprecated).
- **Isolation:** a chat agent never appears in the voice `list-agents`/`v2/list-agents`,
  `GET /v1/admin/profiles` (or its pickers), and can never be dialed, set as a call default,
  used as a `profile_override`, or assigned to a contact. The voice `get-agent`/`update-agent`/
  etc. 404 on a chat id and `get-chat-agent` 404s on a voice id. Retell-LLM ops are
  channel-agnostic (an LLM is shared infra).

## Deferred
- `rerun-chat-analysis` + the `chat_analysis` pipeline → Phase 4c-2.
- `create-chat`/`create-sms-chat` (4a/4b) are NOT tightened to require `channel='chat'` — a chat
  session may still open against a voice agent_id (same-org, RLS-scoped; not a safety leak).
