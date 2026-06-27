# create-sms-chat (Phase 4b-1) — operator note

RetellAI-compatible `POST /compat/create-sms-chat`. Persists an `sms_chat` chat row and
sends the agent's configured greeting via Telnyx. Inert until configured.

## Enable
1. Apply migration `0043` (adds nullable `from_number`/`to_number` to `chat_sessions`).
   Owner-DDL — the deploy migrates as the `usan` owner before `compose up` (see the
   migrations-need-owner runbook).
2. Set in the VM `.env` (already wired into compose) and the Secret Manager env:
   - `TELNYX_MESSAGING_ENABLED=true`
   - `TELNYX_MESSAGING_API_KEY=...`
   - `TELNYX_MESSAGING_PROFILE_ID=...`
   - `TELNYX_FROM_NUMBER=+1...`  (the single provisioned sender)
3. Mint a compat key (super-admin UI) — all compat ops 401 until a key exists.

Until step 2, `create-sms-chat` returns **503**. No `GCP_PROJECT` is needed (no Vertex in 4b-1).

## Behavior
- Request: `from_number` (must equal `TELNYX_FROM_NUMBER`, else 422), `to_number`, optional
  `override_agent_id` / `override_agent_version` / `metadata` / `retell_llm_dynamic_variables`.
- Agent: `override_agent_id` wins; otherwise the `from_number`'s `outbound_sms_agents[0]`
  binding (same-org) is honored; else 422.
- Returns 200 + `ChatResponse` (`chat_type: sms_chat`). The chat is gettable/listable via the
  Phase 4a chat ops. `create-chat-completion` on an sms_chat returns 422.

## Caveats / deferred
- **Orphan window:** if the Telnyx send succeeds but the commit then fails, an SMS was sent
  with no persisted row (tiny window; an idempotency key is deferred).
- **No inbound replies yet:** Phase 4b-2 adds the inbound Telnyx webhook → match the open
  sms_chat (default-org) → Vertex reply → send-back, plus per-message `telnyx_message_id` dedup.
- Multi-tenant cross-org inbound routing, weighted binding selection, and multi-number sending
  are deferred.
