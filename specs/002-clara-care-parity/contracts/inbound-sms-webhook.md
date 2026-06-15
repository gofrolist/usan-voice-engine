# Contract: Inbound Telnyx SMS Webhook

New endpoint — the first inbound-message handler. Lives in `apps/api/src/usan_api/routers/webhooks.py` next to the LiveKit handler. Verification mirrors `livekit_webhooks`/`webhook_signing` (timestamp replay window + signature check) in a new `telnyx_inbound.py`.

---

### POST `/v1/webhooks/telnyx`

**Headers** (Telnyx): `Telnyx-Signature-Ed25519`, `Telnyx-Timestamp`. Verified against the configured Telnyx public key / signing secret; requests failing verification or outside the timestamp drift window are rejected `401/403` before any processing. Rate-limited like other public endpoints.

**Body** (Telnyx `message.received` shape, subset we consume):
```json
{ "data": { "event_type": "message.received",
            "payload": { "from": { "phone_number": "+15551234567" },
                         "to":   [ { "phone_number": "+19499195585" } ],
                         "text": "remind mom to drink water",
                         "id": "telnyx-msg-uuid" } } }
```

**Response**: always `200` after a verified payload is accepted (Telnyx retries on non-2xx); processing is idempotent on the Telnyx message `id`.

**Routing logic** (after verification):
1. Normalize `from` to E.164.
2. **Opt-out first**: if `text` (trimmed, case-insensitive) is an opt-out keyword (`STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`), add `from` to `dnc_list`, send a one-time `opt_out_ack`, notify operator, and stop. (Opt-out from the elder's own number.)
3. **Family task intake**: else, look up `family_contacts` by `from`. If matched, create an `open` `family_tasks` row for the linked elder(s). If the message conflicts with medical safety (heuristic/keyword), set `needs_safety_review` instead of `open`.
4. **Unmatched sender**: if `from` matches neither an elder nor a family contact, do not create a task; record for operator review (safe default, FR-014).

**Idempotency**: dedupe on Telnyx message `id` so retried deliveries don't create duplicate tasks/opt-outs.

**PHI**: inbound text is contact-authored and may name the elder; stored as a family task (already elder-scoped PHI). No outbound PHI here beyond the PHI-minimized `opt_out_ack`.

---

### Settings additions
- `TELNYX_INBOUND_PUBLIC_KEY` / signing secret (SecretStr) — for verification.
- Reuses existing `TELNYX_MESSAGING_*` for the `opt_out_ack` reply.
- The endpoint is live whenever messaging is configured; verification is mandatory regardless of `TELNYX_MESSAGING_ENABLED`.
