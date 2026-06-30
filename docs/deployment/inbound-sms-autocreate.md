# Inbound SMS Auto-Create (Phase 4b-3)

When an inbound SMS arrives at a provisioned DID that carries an `inbound_sms_agents`
binding and matches no open chat, the system auto-creates an `sms_chat` and runs one agent
reply turn. The created chat is retrievable via the existing get-chat / list-chats — there
is no new endpoint.

## Ships inert

Disabled by default. No behavior changes until activated. No `v*` tag is cut by this phase.

## Activation order

1. Provision the inbound DID with an `inbound_sms_agents` binding (the first entry's
   `agent_id` is used; the agent must be a published, ACTIVE profile).
2. Ensure the reply path is configured: `TELNYX_MESSAGING_ENABLED=true` + the three Telnyx
   messaging secrets + `GCP_PROJECT` (Vertex). Without these the handler still OWNS a bound
   DID's inbound but logs `sms_autocreate_unconfigured` and creates nothing.
3. Set `TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED=true` in BOTH the compose `api` `environment:`
   map AND the VM `.env` (the compose-env-passthrough two-place rule), then redeploy/reboot
   so the VM `.env` is refreshed from Secret Manager.

It is independent of `TELNYX_INBOUND_SMS_REPLY_ENABLED` so it can be staged/rolled back on
its own.

## Precedence

STOP/opt-out is honored first (a STOP from anyone is never auto-created). An open
outbound-originated `sms_chat` is handled by the 4b-2 reply engine. A known family contact
falls through to family-task intake (the caregiver relay is never hijacked). Auto-create
fires only for an unknown sender to a bound DID.

## Security / PHI

`organization_id` is server-set by RLS (the seeded default org). Logs carry only the Telnyx
`message_id` + exception type names — never message text, the reply, the agent id, or a raw
phone number. Metrics: `WEBHOOKS_TOTAL{type="telnyx_sms", outcome=...}` with outcomes
`sms_autocreate` / `sms_autocreate_dedup` / `sms_autocreate_unconfigured` /
`sms_autocreate_failed`.

## Known limitation

Single-org only. An inbound DID is attributed to the seeded default org; cross-org DID->org
routing (a `SECURITY DEFINER` DID lookup) is deferred to a future phase, to be built when a
second org has live inbound SMS.
