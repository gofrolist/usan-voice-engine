# Clara Care Parity — Go-Live Compliance Gate

**Feature:** `specs/002-clara-care-parity`
**Constitution:** Principle II — PHI Containment (NON-NEGOTIABLE)
**Status:** ⛔ BLOCKING for any family-facing SMS feature until resolved.

This feature adds the first **outbound family-facing SMS** path (crisis/missed-call
alerts — US2, monthly family report — US8, opt-out acknowledgements — US7) and the
first **inbound** Telnyx SMS webhook (family task intake / `STOP`). Both ride the same
Telnyx account already carrying voice PHI for this product.

## Gate 1 — Telnyx Messaging must be BAA-covered (research Decision 9)

Family alerts inherently signal *something* about an elder (e.g. "we couldn't reach
your mom — please check in"). Even PHI-minimized, this is PHI-adjacent and transits
Telnyx Messaging.

**Required before any family-SMS feature ships to production:**

- [ ] Confirm in writing that the executed Telnyx BAA **covers the Messaging product**
      (not only voice/SIP). Record the confirmation reference here:
      `__________________________________________`
- [ ] If the BAA does **not** cover Messaging: keep every family-SMS feature behind its
      poller flag, **disabled**, until resolved:
      - `TELNYX_MESSAGING_ENABLED=false` — **master carrier-send switch** (defense in depth):
        even if a poller below is accidentally enabled, the notification outbox transmits
        **nothing** while this is false — rows safely backlog as `pending`
        (`notification_outbox.py` line ~50; covered by
        `test_outbox_leaves_pending_when_messaging_disabled`). This is the single flag that,
        held off, guarantees no family/elder SMS leaves the system.
      - `NOTIFICATION_OUTBOX_ENABLED=false` (US2 alerts, US7 opt-out ack, US8 report SMS)
      - `FAMILY_REPORT_POLLER_ENABLED=false` (US8 monthly report — enqueues via the outbox)
      All default OFF in `apps/api/.../settings.py`: the two poller flags above are asserted
      by `test_clara_parity_settings_defaults`, and `TELNYX_MESSAGING_ENABLED` defaults False
      independently (`settings.py`, the Phase-3 messaging block). The rest of the feature
      (crisis escalation flags, schedule slots, memory, callback auto-dial — which sends no
      SMS) ships normally — only the carrier-SMS leg is gated.

The inbound webhook (`/v1/webhooks/telnyx`) is signature-verified and only **ingests**
contact-authored text into elder-scoped storage; it does not egress new PHI beyond the
PHI-minimized `opt_out_ack`. It may be live independent of Gate 1, but the
`opt_out_ack` reply rides the same channel, so it inherits the same flag in practice.

## Gate 2 — Family SMS bodies must be PHI-minimized

All family-facing SMS bodies are built from **system templates that carry no clinical
content** (no mood/pain scores, no medication names, no transcript text). The body says
"please check in" / "we have an update," never the detail.

**Enforced by:**

- `apps/api/src/usan_api/notifications.py` — builds bodies from fixed, PHI-free templates.
- A test (T083) asserts family SMS bodies contain none of the known clinical fields.
- Summarization / report generation runs on **Vertex AI via ADC** (not the Gemini
  Developer API); the generated narrative stays in Postgres and is **not** sent verbatim
  over SMS — only a PHI-minimized notification is texted.

## Related secret-management gate (Constitution + deploy procedure)

`TELNYX_INBOUND_PUBLIC_KEY` (and the poller flags) must reach the VM `.env`
(via IAP-SSH or VM reboot) **before** cutting the `v*` deploy tag — the tag deploy does
not re-fetch secrets. See tasks T086.
