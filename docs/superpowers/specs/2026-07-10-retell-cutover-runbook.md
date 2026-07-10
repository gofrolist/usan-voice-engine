# RetellAI → self-hosted cutover runbook

**Date:** 2026-07-10
**Owner:** voice-engine + usan-retirement-backend
**Companion specs:** `~/gofrolist/usan-retirement-backend/VOICE_PROVIDER_MIGRATION_SPEC.md` (the
contract), Surface 3 external-tools (`2026-07-09-retell-parity-surface3-external-tools-design.md`),
Surface 2A inbound-router (`2026-07-09-retell-parity-surface2a-inbound-router-design.md`).

This is the operational plan to move live traffic from RetellAI to the self-hosted voice engine
**without a hard switch**: resolve the open contract questions, flip feature flags in a safe
order, canary a narrow flow, then widen to 100% and port numbers. It assumes all four API
surfaces are merged (PRs #169 Surface 3, #170 Surface 2A; Surfaces 1 & 2B already shipped) and the
client-side `VOICE_API_BASE` refactor is merged (backend PR #15).

---

## 0. Preconditions (all true before canary)

- [ ] Voice-engine deployed and reachable; `/health` green with the deployed version.
- [ ] The **agent profiles** exist and are **published** in the voice engine (Companion, Sales,
      Inbound, + Betty/QA), each with the migrated prompt + tools (**bucket B — delivered by the
      seed in voice-engine PR #172**: `apps/api/scripts/seed_retell_profiles/`). Prompts and tool
      decls are checked into the client repo (`prompts/*_retell.txt`, `retell/{sales,inbound,
      companion}/`), so B was **not** blocked on a dashboard export. What still needs the client's
      confirmation is voice/LLM *values* only (voice id, model/temperature) + KB binding — the
      seed ships flagged defaults (see the seed README "CONFIRM before production").
- [ ] Telnyx numbers reachable by the engine (SIP inbound trunk + dispatch rule to the agent),
      OR a test DID for canary.
- [ ] The client backend on the `VOICE_API_BASE` refactor (PR #15), `VOICE_API_BASE` **still
      unset** (so it points at Retell — no behavior change yet).

---

## 1. Open questions → recommended answers (client sign-off)

These are the migration spec §10 items. Each has a recommendation and the code reality behind it;
the client confirms or overrides before we flip anything.

| # | Question | Recommendation | Why / code reality |
|---|---|---|---|
| Q1 | **`agent_id`: reuse or remap?** | **Remap the client env** to our ids. | Our ids are UUID-derived (`agent_<hex>`, `compat/ids.py`) — they cannot equal Retell's. We create the 3 profiles, hand over their `agent_<hex>` ids, and the client sets `RETELL_SALES_AGENT_ID` / `RETELL_COMPANION_AGENT_ID` / `RETELL_BETTY_AGENT_ID` to them. Surface 2A's router returns these same ids; the engine decodes + validates them. |
| Q2 | **Webhook signature format** — confirm `v={ms},d={hex_sha256}` over `rawBody+ts`? | **Confirmed, byte-exact.** | `compat/webhook_signature.py` reproduces the Retell SDK: `d = hex(HMAC_SHA256(secret, rawBody + str(ts_ms)))`, header `v={ts_ms},d={d}`, ±5-min window. ⚠️ **Key coordination:** our HMAC key is a **dedicated per-subscription secret** (`CompatWebhookEndpoint.secret`), not `RETELL_API_KEY`. The client's `verify-webhook.ts` uses `RETELL_API_KEY` as the verify key → **set the subscription secret = the client's `RETELL_API_KEY`** (or repoint their verify key). |
| Q3 | **AMD / voicemail** — do we set `in_voicemail=true` and/or `disconnection_reason="machine_detected"`? | **We set `in_voicemail=true`; `disconnection_reason="voicemail_reached"` (not the string `machine_detected`).** | `call_serializer._build_analysis` sets `in_voicemail = (status == VOICEMAIL_LEFT)`. The client's `determineStatus` reads **either** `in_voicemail===true` **or** `disconnection_reason==="machine_detected"`, so voicemail is detected via `in_voicemail`. ✅ works as-is. Confirm the client relies on `in_voicemail` (not exclusively the `machine_detected` string anywhere else). |
| Q4 | **Answered threshold** — is `duration_ms` real talk time (so `>= 10000` = answered)? | **Yes.** | `duration_ms` = `duration_seconds*1000`, computed `answered_at → ended_at` (real conversation), not ring time. Matches the client's `>= 10000` rule. |
| Q5 | **`user_sentiment`** (not in §10 but surfaced in validation) | **Gap — decide: compute it, or accept transcript fallback.** | `CallAnalysis.user_sentiment` exists in the schema but `_build_analysis` leaves it **`None`** — we do **not** emit sentiment today. The client's CRM/crisis logic substring-matches `user_sentiment`; with null it **falls back to the transcript**. Acceptable for v1 if the client confirms; otherwise a small follow-up to populate it. |
| Q6 | **Recording URL TTL / archival** | **Confirm the fetch window; default is 10 min.** | `recording_url` is a GCS presigned URL, TTL = `RECORDING_SIGNED_URL_TTL_S` (**default 600 s**). If the client's `call_ended` consumer may fetch later than 10 min, either raise the TTL for webhook-delivered URLs or archive server-side before delivery. Old Retell recording URLs die at cutover — archive any needed for compliance **before** decommissioning Retell. |
| Q7 | **Number porting** — timeline + provider? | **Start earliest; longest lead time.** | Telnyx port or SIP trunk. Until ported, canary runs on a **test DID**; production numbers move in the final step. |
| Q8 | **`RETELL_FROM_PHONE` vs `RETELL_FROM_NUMBER` gotcha** | **Set both.** | `signup-lead` reads the outbound number from `RETELL_FROM_NUMBER`; the other 4 functions read `RETELL_FROM_PHONE`. Both must be set to the same working number or signup calls break. |

---

## 2. Flag & env sequencing

All voice-engine compat features ship **inert** (default-off) and are activated per-flow. Flip in
this order — each is independently reversible.

### 2.1 Voice-engine (server) — turn features on

| Order | Flag / env | Set to | Enables |
|---|---|---|---|
| 1 | `COMPAT_EXTERNAL_TOOLS_ENABLED` | `true` | Surface 3 — agent calls the client's edge functions. |
| 1 | `COMPAT_TOOL_ALLOWED_HOSTS` | `mrnlotdwthdqcaicwyql.supabase.co` | Egress allow-list (the single validated host). |
| 1 | `COMPAT_TOOL_CALLER_SECRET` | = client `RETELL_FUNCTION_SECRET` | `X-Caller-Secret` on tool calls. |
| 2 | `COMPAT_INBOUND_ROUTER_ENABLED` | `true` | Surface 2A — consult the client router on inbound. |
| 2 | `COMPAT_INBOUND_ROUTER_URL` | `https://<proj>.supabase.co/functions/v1/inbound-call-router` | Router endpoint. |
| 2 | `COMPAT_INBOUND_ROUTER_CALLER_SECRET` | (optional) | `?caller_secret=` — only once the client wires `verifyCaller`. |
| 3 | `COMPAT_WEBHOOK_DELIVERY_ENABLED` | `true` | Surface 2B — deliver `call_ended`/`call_analyzed`. |
| 3 | `COMPAT_WEBHOOK_ALLOWED_HOSTS` | client Supabase host | Webhook egress allow-list. |
| 3 | subscription `secret` | = client `RETELL_API_KEY` | HMAC key parity (Q2). |

### 2.2 Client backend (usan-retirement-backend) — switch + enforce

| Order | Env | Set to | Effect |
|---|---|---|---|
| A | `RETELL_API_KEY` | voice-engine API key / bearer | Surface 1 auth + (Q2) the value the engine signs with. |
| A | `RETELL_*_AGENT_ID` | our `agent_<hex>` ids (Q1) | Route to our agents. |
| A | `RETELL_FROM_PHONE` + `RETELL_FROM_NUMBER` | working outbound number (Q8) | Both set. |
| B | **`VOICE_API_BASE`** | voice-engine base URL | **The switch.** Flips all 12 call sites at once (PR #15). |
| C | `ENFORCE_WEBHOOK_SIGNATURES` | `true` | After signatures verified stable (§5.4). |
| C | `ENFORCE_CALLER_AUTH` | `true` | After tool calls confirmed sending the secret. |

> `VOICE_API_BASE` is the single traffic switch. Everything above it can be staged with traffic
> still on Retell; nothing changes until B flips. `ENFORCE_*` (C) is turned on **last**, only after
> the grace-mode logs show valid signatures/secrets — never on day one.

---

## 3. Cutover sequence (no hard switch)

Follows migration spec §9.

1. **Backend refactor merged (done, PR #15).** `VOICE_API_BASE` unset ⇒ behavior unchanged.
2. **Stand up + configure the engine.** Preconditions §0; flip §2.1 flags on; agents published.
3. **Canary — one narrow flow.** Pick a low-risk slice (e.g. Companion morning-calls for a lead
   subset, or a test DID for inbound). Split mechanism TBD with the client (lead flag / percentage).
   The existing `canary` function is a synthetic health-check, **not** a traffic splitter — reuse it
   to smoke-test the engine, but the real split is a backend routing rule.
   **Compare on the canary:** transcripts, agent latency, webhook status parity, tool-call
   correctness (flat args, no `args` wrapper), voicemail detection.
4. **Enable enforcement (§2.2 C)** once signatures + caller secrets are stable in grace-mode logs.
5. **Widen by flow:** sales → companion → care-calls → QA/Betty. Watch acceptance metrics (§4) at
   each step; hold/roll back on regression.
6. **Archive recordings + port numbers (Q6, Q7).** Archive any compliance-needed Retell recordings
   **before** decommissioning; port production DIDs to Telnyx/SIP.
7. **Full cutover:** `VOICE_API_BASE` at 100%.

### ⚠️ The `failed_jobs` drain gate (spec §9 gotcha)

Before flipping `VOICE_API_BASE` to 100%: `failed_jobs.endpoint_url` is stored **absolute** and
`retry-failed-jobs` replays it verbatim. Any **voice** job enqueued before the flip retries against
**Retell**, bypassing `VOICE_API_BASE`. **Drain or rewrite pending voice jobs before the final
flip.** (The queue is usually empty — dispatchers don't enqueue on Retell error — but verify:
`select count(*) from failed_jobs where endpoint_url like '%retellai.com%' and status='pending'`.)

---

## 4. Acceptance criteria (freeze before routing traffic — spec §8)

Retell's edge is call quality, not the API. Agree thresholds with the client **before** canary:

- **Latency** — agent response p95 < N ms (client sets N from current Retell baseline).
- **AMD accuracy** — correct `in_voicemail` on machines, no false-positive on live elderly callers.
- **Turn-taking** — the agent doesn't talk over the caller.
- **Tool-call stability** — correct flat args, every tool reaches the right edge function.
- **Webhook delivery** — `call_ended` on **100%** of calls (the CRM source of truth).

---

## 5. Rollback

Every step is reversible without a redeploy:

- **Instant traffic rollback:** unset / repoint `VOICE_API_BASE` → back to Retell (Surface 1 & get-call).
- **Per-feature:** flip any `COMPAT_*_ENABLED` off → that surface goes inert (inbound degrades to
  the default agent; tools/webhooks stop).
- **Enforcement:** set `ENFORCE_*=false` → back to grace mode (log, don't reject).
- Keep Retell live (numbers not yet ported, key valid) until acceptance holds at 100%.

---

## 6. Sign-off checklist

- [ ] §1 Q1–Q8 answered/confirmed by the client.
- [ ] §4 acceptance thresholds agreed.
- [ ] Bucket B: agent profiles seeded + published (PR #172, `seed_profiles.py --apply`); the seed
      README "CONFIRM before production" items settled with the client (voice id, LLM model/
      temperature, KB binding for Sales/Inbound, short operational lines).
- [ ] Subscription secret = client `RETELL_API_KEY` (Q2); `COMPAT_TOOL_CALLER_SECRET` = `RETELL_FUNCTION_SECRET`.
- [ ] `failed_jobs` voice queue drained (§3 gate).
- [ ] Compliance recordings archived (Q6).
- [ ] Number port scheduled (Q7).
