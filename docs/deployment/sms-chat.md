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

## Inbound two-way replies (Phase 4b-2)

Phase 4b-2 adds a Telnyx inbound webhook that matches an inbound SMS to an open `sms_chat`
session, generates an agent reply via Vertex, and sends the reply back to the recipient.

### Enable

1. Apply migration `0044` (adds `provider_message_id` dedup column to `chat_messages`).
   Owner-DDL — the deploy migrates as the `usan` owner before `compose up`.
2. All prerequisites from Phase 4b-1 must be satisfied (`TELNYX_MESSAGING_ENABLED=true` +
   the three messaging secrets + migration `0043`).
3. Additionally set in the VM `.env` and Secret Manager env:
   - `TELNYX_INBOUND_SMS_REPLY_ENABLED=true` (default: **off** — webhook acks 200 but takes no
     action until this flag is set)
   - `GCP_PROJECT=<project-id>` — required for Vertex LLM reply generation
   - `TELNYX_INBOUND_PUBLIC_KEY=<ed25519-public-key>` — used to verify Telnyx webhook signatures
4. Register the inbound webhook URL in Telnyx: `POST /compat/webhooks/telnyx-inbound-sms`
   (no compat key required — signature verification uses `TELNYX_INBOUND_PUBLIC_KEY`).

Until `TELNYX_INBOUND_SMS_REPLY_ENABLED=true`, the webhook always acks **200** immediately
and records no reply.

### Behavior

- Telnyx delivers inbound SMS as a signed POST to the webhook endpoint.
- The engine looks up an open `sms_chat` session by `(our_number, recipient)`. If none is
  found, the message is dropped with outcome `sms_reply_unconfigured` (unknown-recipient
  auto-create is deferred to Phase 4b-3).
- Duplicate `provider_message_id` values are ignored (`sms_reply_dedup`).
- A Vertex reply is generated via the session's agent configuration and sent back via
  `telnyx_messaging.send_sms`. The reply is persisted as a `role='agent'` `chat_messages` row.
- The inbound message is persisted as a `role='sms'` `chat_messages` row and is visible in
  `get-chat` / `list-chats` responses (both roles conform to the RetellAI oracle
  `SmsMessage` / `ChatResponse` schemas).

### Observability

The webhook **always acks 200** — failures are never retried by Telnyx. Outcomes are recorded
via the `WEBHOOKS_TOTAL` Prometheus counter (`type="telnyx_sms"`):

| `outcome` label       | Meaning                                              |
|-----------------------|------------------------------------------------------|
| `sms_reply`           | Inbound message matched a session; reply sent        |
| `sms_reply_dedup`     | Duplicate `provider_message_id`; message ignored     |
| `sms_reply_unconfigured` | No open session found for `(our_number, recipient)` |
| `sms_reply_failed`    | Reply generation or send raised an exception         |

### Caveats / deferred

- **Unknown-recipient auto-create:** if no open session exists for the inbound `(our_number,
  recipient)` pair, the message is silently dropped. Auto-creating a new session on first
  inbound is deferred to Phase 4b-3.
- Multi-tenant inbound routing (matching by org) is deferred.
