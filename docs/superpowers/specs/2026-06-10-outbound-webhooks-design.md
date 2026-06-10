# Signed Outbound Event Webhooks — push call-lifecycle events to operator systems (design)

**Date:** 2026-06-10
**Status:** Final — three-lens review (security/SSRF/PHI, correctness, ops-completeness) applied
**Phase:** A3
**Predecessors:** Calls console + ops queues (A2, branch `feat/calls-ui`, alembic head `0013`); Batch & scheduled calling (A1, PR #55); Admin-UI Phase 3 tools + SMS outbox (PR #54, merged `ef2fa81`). This spec's branch `feat/outbound-webhooks` (@ `311fc22`) is stacked on `feat/calls-ui`; migration `0014` chains on `0013`.
**Related specs:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md`, `docs/superpowers/specs/2026-06-09-admin-ui-phase3-tools-design.md`, `docs/superpowers/specs/2026-06-10-batch-scheduled-calling-design.md`, `docs/superpowers/specs/2026-06-10-calls-ui-ops-queues-design.md`

> **Review deltas vs the original brief (flagged loudly):** (1) `flag.created` no longer carries
> `category` or `elder_id` — review showed that a health-category enum keyed to the stable
> production `elder_id` is individually-identifiable health information for the intended
> recipient, which broke the load-bearing "PHI-free tier / receivers need no BAA" claim. The
> paging use case survives on `severity` + opaque ids (§6.4, §8.3). (2) The claimed
> "every terminal mutator is already status-guarded" invariant was verified **false**; this
> phase hardens the enqueue-bearing mutators to atomic guarded transitions as a prerequisite
> (§2.1). (3) `batch.completed` is emitted only from the phase-6 drain stamp (both statuses),
> not from the cancel endpoint, whose payload was unconstructible (§6.6).

---

## 1. Goals & non-goals

### 1.1 Goals

Let operator systems **react to call lifecycle without polling** `GET /v1/calls`. RetellAI parity reference: their `call_started` / `call_ended` / `call_analyzed` webhooks with an `X-Retell-Signature` HMAC header. Our mapping: `call.started` ≈ call_started, `call.completed` ≈ call_ended; `call_analyzed` has **no equivalent this phase** (no transcript/analysis events — see PHI posture). We add three events Retell doesn't have because our ops model is richer: `flag.created` (with bounded severity, so a care system can page on `urgent` without waiting for a human to read the Grafana alert), `callback.created`, and `batch.completed`.

Closed event enum (five values): **`call.started`, `call.completed`, `flag.created`, `callback.created`, `batch.completed`.** `call.completed` fires for **every** terminal status — `completed`, `voicemail_left`, `no_answer`, `busy`, `failed`, `dnc_blocked`, `cancelled` — carrying the end status plus attempt/chain linkage (`attempt`, `parent_call_id`, `origin`), so a consumer can distinguish "answered and finished" from "chain attempt 2 went to voicemail, a retry may follow."

**PHI posture is the crux of this design.** Operator-configured egress URLs are the exact hazard class the Phase 3 tools design rejected for agent tools: an arbitrary HTTPS destination chosen at runtime by configuration, not by code review. The single payload tier this phase is **PHI-free by construction**: opaque ids (`call_id`, `elder_id`, `batch_id`, `flag_id`, `callback_id`), bounded enums (status, direction, origin source, severity, event type), numerics (`duration_seconds`, `attempt`, target counts), and ISO-8601 UTC timestamps. **No** names, phone numbers, flag reasons or categories, callback notes/requested-time text, `dynamic_vars`, transcripts, recording URLs, or `end_reason`. **Refinement from review:** `flag.created` additionally excludes `elder_id` and the health-domain `category` enum — a health classifier keyed to a stable person identifier is not defensibly de-identified for a recipient that also holds API access; the event carries `severity` keyed only to opaque `flag_id`/`call_id`, and "who/what" requires an authenticated API fetch. This payload minimization is what bounds the blast radius of a misconfigured or compromised endpoint and keeps the BAA boundary intact: the webhook channel itself egresses no free text, no direct identifiers, and no person-linked health categories. A richer PHI tier (restoring `category`/`elder_id` under an explicit gate) is deferred to the Phase C compliance-mode work (open Q5).

Delivery is **transactional-outbox + poller**: events are enqueued in the same DB transaction as the state change they announce, and a fourth lifespan poller delivers them with signed POSTs, a retry ladder, and a per-endpoint circuit breaker. The feature ships **inert** (`WEBHOOK_DELIVERY_ENABLED=false` in prod), matching the A1 scheduler's staged-enable pattern.

### 1.2 Rejected alternatives

- **POST-after-commit (no outbox)** — the dual-write problem: a crash between commit and POST silently loses the event; POST-before-commit emits phantom events for rolled-back transactions. An outbox row in the same commit makes enqueue exactly-once (after the §2.1 mutator hardening); delivery is then at-least-once with retries. This is the same reasoning the SMS outbox header comment (`sms_outbox.py:10–15`) already canonizes.
- **BackgroundTasks-only delivery (the SMS shape)** — `flush_pending_sms` is fire-once with no sweeper; rows stranded by a crashed BackgroundTask stay stranded. Acceptable for SMS, not for webhooks that need a backoff ladder and `next_attempt_at`. Also, several enqueue sites (livekit_dispatch, both orchestrators) run outside any request context, so FastAPI `BackgroundTasks` isn't even available there.
- **Piggybacking the retry poller or scheduler cycle** — the retry poller is default-ON and dials phones; the scheduler is gated by a cross-field concurrency validator. Coupling "deliver HTTP POSTs" to "place calls" entangles blast radii and flags. The house already treats "another poller" as cheap precedent (schedule_orchestrator docstring: "the third poller").
- **LISTEN/NOTIFY** — adds a second delivery trigger mechanism with no durability win over the poller; the poller interval (10 s) is well within "react to call lifecycle" latency needs.
- **Operator-supplied signing secrets** — rejected; server-generated 32-byte secrets prevent weak keys and accidental secret reuse across systems.
- **Cancel-endpoint `batch.completed` emission** — rejected during review: at cancel time `completed_at` is NULL (stamped later by phase 6), the histogram is non-final (in-flight chains settle afterwards), and the endpoint is idempotent (re-cancel would double-emit). The event fires once, from the phase-6 drain stamp, for both statuses (§6.6).

### 1.3 Non-goals (this phase)

- Per-event payload customization or field selection.
- Transcript / analysis / PHI payload tiers, including `category`/`elder_id` on `flag.created` (Phase C compliance mode, open Q5).
- An immediate `batch.cancelled` acknowledgment event — `batch.completed{status=cancelled}` fires at settlement, not at cancel-API time; the cancel API response is the immediate ack (open Q11).
- Admin-UI surface for endpoint CRUD (operator API only, like A1).
- Zapier-style integration templates.
- Workspace/org scoping of endpoints (Phase B adds `org_id`).
- A `call.chain_settled` event (the phase-1 finalizer in `schedule_orchestrator.py:193–234` is the natural future hook; consumers can reconstruct chains from `parent_call_id`/`attempt` today — open Q4).
- Sub-poll-interval delivery latency (event-driven nudge — open Q3).
- Multi-replica delivery workers (single-replica documented, same as the rate limiter and trunk auto-provisioning).

## 2. Architecture

```
state change (~20 caller commit sites funneled through 11 instrumented
mutators/creators: call terminal transitions, answered, flag/callback
creation, batch drain-settlement)
   │  same transaction ───────────────────────────────┐
   ▼                                                   ▼
business row UPDATE/INSERT                webhook_deliveries INSERT (outbox,
   │                                       one row per enabled+subscribed endpoint)
   └── caller's db.commit() ── atomically commits both ──┘
                                                       │
webhook_delivery.run_poller  ← NEW, 4th lifespan poller (ALWAYS runs;
   │                            WEBHOOK_DELIVERY_ENABLED gates delivery only —
   │                            housekeeping/sweeps/gauge run regardless)
   │  claim due rows (FOR UPDATE SKIP LOCKED, attempt-bump = crash-safe lease)
   │  group by endpoint → asyncio.gather across endpoints (no head-of-line)
   │  SSRF re-check (DNS resolve → public-IP gate, fail-closed)
   ▼
httpx POST (10s timeout, redirects OFF, HMAC-SHA256 signed)
   │  2xx → delivered;  else → ladder 1m/5m/30m → failed (max 4 attempts)
   └─ per-endpoint circuit breaker (atomic SQL increment) → auto-disable
      + WARN + metric + dedicated alert rule

operator (OPERATOR_API_KEY bearer, rate-limited)
   ├─ POST/GET/PATCH/DELETE /v1/webhook-endpoints      routers/webhook_endpoints.py
   ├─ POST /v1/webhook-endpoints/{id}/test (signed ping)
   ├─ GET  /v1/webhook-endpoints/{id}/deliveries
   └─ POST /v1/webhook-deliveries/{id}/redeliver
```

### 2.1 Transactional-outbox rationale (the load-bearing decision)

Every event corresponds to exactly one guarded state transition that already commits atomically (the A1 §6.2 invariant: *terminal transition + `schedule_retry` share ONE commit*). The outbox row joins that same commit: the repo helper inserts + flushes, the caller's existing `db.commit()` makes business change and event durable together. Consequences:

- **No phantom events**: a rolled-back transition enqueues nothing.
- **No lost events**: a committed transition has its rows on disk before any process can crash.
- **Exactly-once enqueue** — *after the hardening below*: each terminal mutator performs exactly one atomically-guarded transition, so one occurrence produces exactly one enqueue.
- **At-least-once delivery**: the poller may duplicate a POST if it crashes between POST and outcome-commit; receivers dedupe on `delivery_id` (now inside the signed body, §7) plus the semantic keys (§7).

**Hardening prerequisite (review finding, HIGH — the draft's invariant was false).** The draft claimed "every terminal mutator is already status-guarded." Verified false: only `cancel_queued_tips` is a guarded SQL UPDATE; `mark_completed_if_in_progress`, `mark_voicemail_left_if_in_progress`, `complete_call_if_in_progress`, `mark_failed_if_active` are unlocked Python read-modify-write (no `with_for_update()`, no `WHERE status` predicate on the flush), and `mark_dial_failure` / `set_status` / `mark_answered` have **no guard at all**. Under READ COMMITTED, two overlapping sessions (e.g. `POST /v1/calls/{id}/outcome` voicemail vs the `room_finished` webhook; `end_call` vs `room_finished`; `reclaim_stuck_dialing` re-queue vs a stale dial task's `mark_dial_failure`) can both pass the Python check, both commit, and emit two `call.completed` occurrences with *different statuses* and distinct delivery ids that no receiver can collapse. Therefore, as part of instrumenting them, every enqueue-bearing mutator becomes an **atomic guarded transition**:

- `mark_completed_if_in_progress`, `mark_voicemail_left_if_in_progress`, `complete_call_if_in_progress`, `mark_failed_if_active` — add `with_for_update()` to the row load (minimal-diff option) so the in-Python status check holds under the row lock; the second racer re-reads the terminal status and returns `None`.
- `mark_dial_failure` — gains a real guard: load `with_for_update()`, transition (and enqueue) only when the current status is non-terminal (`queued`/`dialing`); already-terminal rows return `None` (callers verified tolerant). This closes the reclaim-race double-emit (`reclaim_stuck_dialing` re-queues a stuck DIALING call; the stale dial task's late failure path must become a no-op).
- `set_status` — load `with_for_update()`, capture `old_status` before assignment; enqueue `call.completed` only on a non-terminal→terminal transition (covers dial-time `DNC_BLOCKED` and dispatch-failure `FAILED` call sites with zero call-site edits; terminal→terminal enqueues nothing).
- `mark_answered` — the **whole write**, not just the enqueue, is gated on prior status ∈ {`dialing`, `ringing`} under `with_for_update()` (RINGING is never assigned today; kept as dead-state tolerance). This also fixes the pre-existing zombie bug where a late `mark_answered` could resurrect a `room_finished`-completed call to `IN_PROGRESS` and pin an in-flight slot.
- `cancel_queued_tips` — already a guarded SQL UPDATE; gains `.returning(...)` of the payload columns; one event per returned row.

§10.7 adds genuine two-concurrent-sessions tests for these races — the existing A1 race tests invoke the mutators sequentially and would pass without the fix.

**Integration shape — enqueue inside the repo mutators (shape (a)).** All call-terminal writers are repo functions in `repositories/calls.py` that flush in the caller's session; instrumenting them once covers all ~20 caller commit sites with no per-caller changes and no way for a future caller to forget the event. Specifically:

| Site | Event | Change |
|---|---|---|
| `mark_dial_failure`, `mark_completed_if_in_progress`, `mark_voicemail_left_if_in_progress`, `complete_call_if_in_progress`, `mark_failed_if_active` (repositories/calls.py) | `call.completed` | enqueue inside the now-guarded transition path (hardening above) |
| `set_status` (calls.py) | `call.completed` | old-status capture under lock; enqueue only on non-terminal→terminal |
| `create_call` / `create_materialized_root` when created with `status=DNC_BLOCKED` | `call.completed` | terminal-at-birth enqueue |
| `cancel_queued_tips` | `call.completed` (×N) | guarded bulk UPDATE gains `.returning(...)`; one event per cancelled row |
| `mark_answered` | `call.started` | guarded transition (above); enqueue inside it |
| `create_inbound_call` | `call.started` | inbound calls are answered at birth |
| `create_follow_up_flag` (repositories/follow_up_flags.py) | `flag.created` | enqueue inside the creator, same flush-only discipline |
| `create_callback_request` (repositories/callback_requests.py) | `callback.created` | ditto |
| phase-6 `_complete_drained_batches` (schedule_orchestrator.py) | `batch.completed` (status=`completed` **and** `cancelled`) | the existing `stamped = [(batch.id, batch.status)…]` list drives one event per stamped batch, between the repo call and the commit — the **only** emission point for this event (§6.6) |

The cancel endpoint (`routers/batches.py`) enqueues **nothing** itself: the `cancel_queued_tips` it triggers emits per-call `call.completed{status=cancelled}` events through the mutator, and the batch-level event arrives when phase 6 stamps the drained batch. Re-cancelling an already-cancelled batch therefore cannot double-emit anything.

**Write-amplification bound (accepted, stated):** cancelling a full batch (`MAX_BATCH_TARGETS = 500`) with 10 subscribed endpoints inserts up to 5,000 outbox rows inside the cancel transaction (and similarly in the phase-5 sweep). Bounded by the 500-target and 10-endpoint caps; accepted for this phase rather than chunked.

`VOICEMAIL_LEFT` is terminal-per-attempt and may spawn a retry child in the same commit; `call.completed` is deliberately **per-attempt** — the payload's `attempt`/`parent_call_id`/`origin` let consumers track chains, and a chain-settled event is deferred (open Q4). We do not ship a `will_retry` field: shape (a) enqueues inside the mutator, before the caller's `schedule_retry`, and guessing would be a lie.

The enqueue helper fans out **at enqueue time**: one `webhook_deliveries` row per *enabled* endpoint whose subscription list contains the event. Zero subscribed endpoints → zero rows → the whole feature is a no-op at zero cost (this, plus the delivery flag, is the ship-inert posture). The endpoint table is capped at 10 rows (§8.4), so the fan-out SELECT inside hot mutators is cheap; the cancel-burst worst case is the bound stated above.

### 2.2 New / touched files

| File | Role |
|---|---|
| `apps/api/migrations/versions/0014_outbound_webhooks.py` | NEW — §3 |
| `apps/api/src/usan_api/db/models.py` | TOUCHED — `WebhookEndpoint`, `WebhookDelivery` (TEXT + CHECK convention per 0013, mirrors `SmsMessage`) |
| `apps/api/src/usan_api/webhook_events.py` | NEW — typed payload builders: one Pydantic model per event; **the only place payloads are constructed** (PHI-free allowlist by construction, §6); includes the origin root-walk helper (§6.1) |
| `apps/api/src/usan_api/repositories/webhook_outbox.py` | NEW — `enqueue_event(db, event, payload)` (flush-only, fan-out), `claim_due(db, now, limit)`, `mark_delivered`, `mark_attempt_failed` (guarded on `status='pending'`), `expire_stale_pending`, `sweep_crash_residue`, `prune_old(db)` |
| `apps/api/src/usan_api/repositories/webhook_endpoints.py` | NEW — operator CRUD reads/writes (flush-only; caller owns commit); atomic breaker increment/reset |
| `apps/api/src/usan_api/schemas/webhook_endpoints.py` | NEW — request/response models; registration-time URL validator calls `ssrf_guard`; `events` normalized to a sorted de-duplicated list |
| `apps/api/src/usan_api/ssrf_guard.py` | NEW — `validate_webhook_url(url)` (registration) + `resolve_public_or_raise(host)` (delivery); single source for both layers |
| `apps/api/src/usan_api/webhook_signing.py` | NEW — `sign(secret, ts_ms, raw_body) -> str`; one function, one vector test |
| `apps/api/src/usan_api/webhook_delivery.py` | NEW — `run_poller` / `poll_once` / `deliver_one`; httpx client per the `telnyx_messaging.py` raw-httpx + wrap-errors template; module-level `WebhookDeliveryError` |
| `apps/api/src/usan_api/routers/webhook_endpoints.py` | NEW — §4 routes; sentinel-actor audit rows in same commit on mutations (§4) |
| `apps/api/src/usan_api/repositories/calls.py` | TOUCHED — per §2.1 table + guarded-transition hardening |
| `apps/api/src/usan_api/repositories/follow_up_flags.py`, `callback_requests.py` | TOUCHED — enqueue in creators |
| `apps/api/src/usan_api/schedule_orchestrator.py` | TOUCHED — `batch.completed` enqueue in phase 6 (both statuses) |
| `apps/api/src/usan_api/main.py` | TOUCHED — register router; 4th poller in lifespan `poller_tasks` (always started) |
| `apps/api/src/usan_api/ratelimit.py` | TOUCHED — allowlist `/v1/webhook-endpoints`, `/v1/webhook-deliveries` (§8.4) |
| `apps/api/src/usan_api/settings.py` | TOUCHED — §5.1 settings |
| `apps/api/src/usan_api/observability/custom_metrics.py` | TOUCHED — §9 counters + pending-depth gauge |
| `infra/docker-compose.yml`, `.env.example`, `.env.prod.example`, `infra/docker-compose.prod.yml` | TOUCHED — env plumbing; dev default `true`, prod pinned `false`; inbound-vs-outbound `WEBHOOK_*` comment block |
| `infra/grafana/provisioning/alerting/usan_alerts.yml` | TOUCHED — `usan-webhook-delivery-failed` **and** `usan-webhook-endpoint-auto-disabled` rules |
| `apps/api/tests/conftest.py` | TOUCHED — add both tables to the TRUNCATE list |

## 3. Data model

Two tables. House conventions: raw `op.execute()` SQL, numbered comment sections, TEXT + CHECK for small status sets (the 0013 convention, not PG enums), full `downgrade()` with `IF EXISTS`.

### 3.1 `webhook_endpoints`

Operator-registered destinations. `secret` is server-generated (32 random bytes, hex) and returned exactly once at create; stored plaintext because it must be recoverable to sign every delivery (Cloud SQL encryption at rest; never logged, never returned by reads — §8.3). `events` is a `TEXT[]` subscription list constrained to the closed enum (the schema layer normalizes to a sorted de-duplicated list before insert; the DB CHECK alone would admit duplicates). `consecutive_failures` + `disabled_reason` are the circuit-breaker state (§5.5).

No `UNIQUE(url)` and the 10-endpoint cap is an application-level count — two concurrent creates can momentarily yield 11. Accepted on a rate-limited single-key operator plane; stated here so nobody mistakes the cap for a DB invariant.

### 3.2 `webhook_deliveries`

The transactional outbox, one row per (event occurrence × subscribed endpoint). `payload` is the PHI-free envelope (§6.1). `last_error` stores the exception **type name only**, never `str(exc)` (house rule, sms_outbox.py:31–36). `'ping'` appears in the delivery `event` CHECK but not in the subscription CHECK: test pings are always deliverable regardless of subscriptions.

### 3.3 Migration `0014_outbound_webhooks.py` (exact DDL)

```python
"""outbound webhooks: webhook_endpoints + webhook_deliveries transactional outbox

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Operator-registered webhook destinations. secret = 64 hex chars
    # (32 random bytes), server-generated, returned once at create, never logged.
    # events is a subscription list constrained to the closed event enum;
    # disabled_reason marks circuit-breaker auto-disables (operator disables via
    # enabled=false keep it NULL, so the two are distinguishable).
    op.execute(
        """
        CREATE TABLE webhook_endpoints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            url TEXT NOT NULL,
            description TEXT,
            enabled BOOLEAN NOT NULL DEFAULT true,
            secret TEXT NOT NULL,
            events TEXT[] NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_webhook_endpoints_events CHECK (
                cardinality(events) >= 1
                AND events <@ ARRAY[
                    'call.started', 'call.completed', 'flag.created',
                    'callback.created', 'batch.completed'
                ]::TEXT[]
            ),
            CONSTRAINT ck_webhook_endpoints_disabled_reason CHECK (
                disabled_reason IS NULL OR disabled_reason IN ('circuit_breaker')
            ),
            CONSTRAINT ck_webhook_endpoints_failures CHECK (consecutive_failures >= 0)
        )
        """
    )

    # 2. Transactional outbox: one row per (event occurrence x subscribed endpoint),
    # inserted in the SAME transaction as the state change it announces.
    # 'ping' is valid as a delivery event (the /test endpoint) but is deliberately
    # absent from the subscription CHECK above.
    # last_error: exception TYPE NAME only, never str(exc) (PHI-adjacent rule).
    op.execute(
        """
        CREATE TABLE webhook_deliveries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            endpoint_id UUID NOT NULL
                REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            response_code INTEGER,
            last_error TEXT,
            delivered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_webhook_deliveries_status CHECK (
                status IN ('pending', 'delivered', 'failed')
            ),
            CONSTRAINT ck_webhook_deliveries_event CHECK (
                event IN (
                    'call.started', 'call.completed', 'flag.created',
                    'callback.created', 'batch.completed', 'ping'
                )
            ),
            CONSTRAINT ck_webhook_deliveries_attempts CHECK (attempts >= 0)
        )
        """
    )

    # 3. Claim index: the poller predicate is
    # (status = 'pending' AND next_attempt_at <= now()), so a partial index on
    # pending rows keeps claims O(due) regardless of delivered/failed history.
    op.execute(
        """
        CREATE INDEX idx_webhook_deliveries_due
            ON webhook_deliveries (next_attempt_at)
            WHERE status = 'pending'
        """
    )

    # 4. Operator deliveries list: GET /v1/webhook-endpoints/{id}/deliveries
    # orders newest-first per endpoint; (created_at, id) pair because created_at
    # ties are guaranteed (func.now() is the transaction timestamp and fan-out
    # inserts one row per endpoint in one transaction).
    op.execute(
        """
        CREATE INDEX idx_webhook_deliveries_endpoint
            ON webhook_deliveries (endpoint_id, created_at DESC, id DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_webhook_deliveries_endpoint")
    op.execute("DROP INDEX IF EXISTS idx_webhook_deliveries_due")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS webhook_endpoints")
```

ORM models mirror `SmsMessage` (models.py:394–421): UUID pk with `gen_random_uuid()` server default, `Text` status with `server_default`, `JSONB` payload, `created_at`/`updated_at` with `func.now()` + `onupdate`.

## 4. API surface

All routes live on the **operator-key plane** (`OPERATOR_API_KEY` bearer via router-level `dependencies=[Depends(require_operator_token)]`, the batches precedent), consistent with decision history (schedules/batches in A1) — these configure machine-to-machine egress, not human triage. Both prefixes are added to the rate-limit allowlist (§8.4).

**Audit (corrected from the draft — the cited "A1 `batch_cancelled` precedent" is a log line, not a DB row, and runs after commit).** Every mutation writes an `admin_audit` row in the same commit via `repositories/admin_audit.py:record` with the sentinel `actor_email="operator-api-key"` — this is a **new precedent**, deliberately chosen over the A1 log-line `_audit` because durable DB audit is wanted for egress configuration changes; the sentinel is documented in the model docstring so the admin-session-identity assumption of that column is explicitly broken here, on purpose. Actions: `webhook_endpoint_created|updated|deleted`, `webhook_test_sent`, `webhook_redelivered`. Audit payloads record `endpoint_id` and changed field *names* — never the secret, never the URL query string.

| Route | Purpose |
|---|---|
| `POST /v1/webhook-endpoints` | Create. Body: `{url, description?, events[]}`. URL passes the registration-time SSRF validator (§8.1); `events` deduplicated+sorted. Server generates `secret = secrets.token_hex(32)`. **201 response is the only place the secret ever appears**: `{id, url, description, enabled, events, secret, created_at}`. 422 on invalid URL/events; 422 when 10 endpoints already exist (app-level count, §3.1). |
| `GET /v1/webhook-endpoints` | List. No secret field, ever. Includes `enabled`, `disabled_reason`, `consecutive_failures`, and `pending_deliveries` (per-endpoint pending count — backlog visibility, §9). |
| `GET /v1/webhook-endpoints/{id}` | Detail, same shape (no secret). |
| `PATCH /v1/webhook-endpoints/{id}` | Update `url` (re-validated through the full SSRF gate), `description`, `events`, `enabled`. Setting `enabled=true` resets `consecutive_failures=0` and clears `disabled_reason` (the operator re-arm path for a tripped breaker). |
| `DELETE /v1/webhook-endpoints/{id}` | 204. Deliveries cascade (including pending backlog — the long-disabled-endpoint cleanup path). |
| `POST /v1/webhook-endpoints/{id}/test` | Enqueues a `ping` delivery row (`next_attempt_at = now()`) so the test exercises the *real* pipeline — outbox, claim, SSRF re-check, signing, ladder. 202 `{delivery_id}`. 409 when `WEBHOOK_DELIVERY_ENABLED=false` (a test that can never send is a lie) or the endpoint is disabled. |
| `GET /v1/webhook-endpoints/{id}/deliveries` | Paged (`limit` ≤100 default 50, `offset`), newest first, optional `status`/`event` filters. Returns `{id, event, status, attempts, next_attempt_at, response_code, last_error, delivered_at, created_at, updated_at, payload}` — `updated_at` doubles as the last-attempt timestamp (a `failed` row's `next_attempt_at` is stale and `delivered_at` null); **payload included; it is PHI-free by construction** (§6), which is precisely what makes this debuggability affordance safe. |
| `POST /v1/webhook-deliveries/{id}/redeliver` | Guarded SQL reset: `UPDATE … SET status='pending', attempts=0, next_attempt_at=now(), response_code=NULL, last_error=NULL WHERE id=:id AND status IN ('delivered','failed') RETURNING id` — the status predicate is load-bearing (a Python check would race the poller's in-flight claim). 409 if the row is already `pending`. 429 if the target endpoint already has ≥100 pending deliveries (backpressure; also bounds re-arm storms from a leaked operator key — review L2). The `X-Usan-Delivery-Id`/body `delivery_id` stays stable — it identifies the event occurrence at that endpoint, and receiver-side dedupe on it is exactly what redelivery semantics want. 409 if the endpoint is disabled. 202 `{delivery_id}`. |

No secret-read or secret-rotate endpoint this phase: rotation = delete + recreate (open Q1).

## 5. Delivery worker design

### 5.1 Process model — a fourth poller, always on; the flag gates delivery only

`webhook_delivery.run_poller(stop)` joins retry/retention/scheduler in `main.py` lifespan `poller_tasks`, byte-for-byte loop discipline: `logger.bind(component="webhook_delivery")`, try/except per cycle (logged, never fatal), `asyncio.wait_for(stop.wait(), timeout=interval)` with `contextlib.suppress(TimeoutError)`.

**The poller task always starts.** `WEBHOOK_DELIVERY_ENABLED` gates the *delivery* half of each cycle (claim + POST); the *housekeeping* half (§5.4 sweeps, prune, pending-expiry, the pending-depth gauge) runs every cycle regardless. Review rationale: the draft coupled housekeeping to the flag, so the documented flag-off backlog (§11.4) would accumulate with no sweep, no prune, and no metric. Housekeeping never egresses, so ship-inert ("nothing leaves the box with the flag off") still holds.

Why a separate poller and not piggybacking (decisive, per grounding): the retry poller is default-ON and **dials phones**; the scheduler flag is entangled with the concurrency-gate validator; and several enqueue sites have no request context so the SMS BackgroundTasks shape cannot even trigger there. Pollers are cheap; blast radii stay separate. A BackgroundTasks "nudge" for sub-interval latency is deferred (open Q3) — at a 10 s default interval the poller alone meets the need.

Settings (`settings.py` conventions: `Field(alias=...)`, int bounds; no new optional strings so `_blank_to_none` is untouched; no cross-field validator needed — per-row secrets mean the flag has no startup prerequisite). All keys carry the `WEBHOOK_DELIVERY_` prefix to keep the namespace disjoint from the **inbound** LiveKit-verification `WEBHOOK_MAX_AGE_S`; `.env.example` gains an inbound-vs-outbound comment block:

| Setting | Env var | Default | Bounds |
|---|---|---|---|
| `webhook_delivery_enabled` | `WEBHOOK_DELIVERY_ENABLED` | `False` | — |
| `webhook_delivery_poll_interval_s` | `WEBHOOK_DELIVERY_POLL_INTERVAL_S` | `10` | 5–300 |
| `webhook_delivery_timeout_s` | `WEBHOOK_DELIVERY_TIMEOUT_S` | `10` | 1–60 |
| `webhook_delivery_circuit_breaker_threshold` | `WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD` | `10` | 1–100 |

### 5.2 Claim — attempt-bump as a crash-safe lease

One short transaction claims up to 20 due rows and **pre-schedules their next attempt**, so a worker crash mid-POST needs no reclaim sweeper — the row simply comes due again at the next ladder rung:

```sql
WITH due AS (
    SELECT d.id
    FROM webhook_deliveries d
    JOIN webhook_endpoints e ON e.id = d.endpoint_id AND e.enabled
    WHERE d.status = 'pending'
      AND d.next_attempt_at <= now()
      AND d.attempts < 4
    ORDER BY d.next_attempt_at
    LIMIT 20
    FOR UPDATE OF d SKIP LOCKED
)
UPDATE webhook_deliveries w
SET attempts = w.attempts + 1,
    next_attempt_at = now() + (CASE w.attempts + 1
        WHEN 1 THEN interval '1 minute'
        WHEN 2 THEN interval '5 minutes'
        ELSE interval '30 minutes' END),
    updated_at = now()
FROM due
WHERE w.id = due.id
RETURNING w.id, w.endpoint_id, w.event, w.payload, w.attempts;
```

The claim commits immediately — **no row locks are held across HTTP POSTs**. The join on `e.enabled` means rows for breaker-disabled endpoints are simply not claimed; they resume (with their attempt count) when the operator re-enables.

**No head-of-line blocking (review fix):** claimed rows are grouped by `endpoint_id`; groups are delivered concurrently via `asyncio.gather`, sequential (oldest-first) *within* a group to preserve per-endpoint ordering. A hanging receiver therefore delays only its own group, not every endpoint's deliveries; worst case for the hanging endpoint itself is bounded by its group size × timeout, and the per-delivery `enabled` re-check below stops a group early once its breaker trips mid-cycle.

### 5.3 Per-row delivery + per-row commit

For each claimed row, in its own short transaction afterward (sms_outbox discipline — outcome committed per row, bounding the at-least-once duplicate window to one row):

1. Load endpoint URL + secret + `enabled`. If the breaker tripped mid-cycle (`enabled=false`), skip the row without POSTing — its already-bumped `next_attempt_at` re-offers it later if re-enabled. Otherwise run the delivery-time SSRF resolver gate (§8.2). On SSRF rejection: record the attempt failure with `last_error='SsrfBlocked'` and increment the breaker.
2. Serialize the canonical body **at send time** (§7 — JSONB does not preserve byte form, so we sign what we send; the body gains `delivery_id` at this point), compute the signature, POST with `httpx.AsyncClient(timeout=settings.webhook_delivery_timeout_s, follow_redirects=False)`. Errors wrapped per the `telnyx_messaging.py` template: `except (httpx.HTTPError, ValueError) as exc: raise WebhookDeliveryError(...) from exc`; only `type(exc).__name__` survives into `last_error`/logs.
3. **2xx** → `mark_delivered` (guarded `UPDATE … WHERE id=? AND status='pending' RETURNING id`, idempotent across races): `status='delivered'`, `delivered_at`, `response_code`; endpoint `consecutive_failures=0` (atomic SQL `SET consecutive_failures = 0`). Commit; then increment `usan_webhook_deliveries_total{event,outcome="delivered"}` (increment-after-commit, house rule).
4. **Anything else** (non-2xx incl. 3xx, timeout, transport error, SSRF block) → `mark_attempt_failed`, guarded on `status='pending'` exactly like `mark_delivered` (review L3): record `response_code` (if any) + `last_error` type name. If `attempts >= 4`: `status='failed'`. Increment endpoint `consecutive_failures` (atomic SQL increment, §5.5); threshold reached → trip the breaker. Commit, then metrics.

**Outcome-label rule (review fix — keeps the alert honest):** the **terminal** attempt always emits `outcome="failed"`, regardless of failure mode — including SSRF blocks (`last_error='SsrfBlocked'` is preserved for diagnosis). Non-terminal attempts emit `outcome="retry_scheduled"` for ordinary failures and `outcome="ssrf_blocked"` for resolver-gate rejections. Without this rule, a permanently-private-resolving endpoint would burn all four attempts as `ssrf_blocked` and the `outcome="failed"` alert would never fire.

### 5.4 Retry ladder, crash semantics & housekeeping

Max **4 attempts** per delivery: immediate, +1 m, +5 m, +30 m, then `failed`. Crash anywhere after claim-commit → the bumped `next_attempt_at` re-offers the row automatically; crash after POST but before outcome-commit → duplicate POST next rung (documented at-least-once; receivers dedupe on `delivery_id`).

Housekeeping runs in the always-on poller (flag-independent, §5.1) on an **hourly cadence** (monotonic-clock check inside the loop — the draft's every-10 s sweep was a pointless seq scan; retention precedent is daily):

- `sweep_crash_residue` — a crash on a row already at `attempts=4` leaves it `pending` but unclaimable (`attempts < 4` predicate): `UPDATE … SET status='failed', last_error=COALESCE(last_error, 'crash_residual') WHERE status='pending' AND attempts >= 4 AND updated_at < now() - interval '10 minutes'` (`COALESCE` so a genuine last error type is not overwritten — review L4d).
- `expire_stale_pending` (review fix — pending rows for disabled endpoints and flag-off backlogs previously escaped every cleanup path): `UPDATE … SET status='failed', last_error='expired' WHERE status='pending' AND created_at < now() - interval '7 days'`. This bounds outbox growth from endpoints registered while delivery is off, breaker-disabled endpoints, and operator-disabled endpoints, and bounds how stale an `occurred_at` a receiver can ever see to 7 days.
- `prune_old` — `DELETE FROM webhook_deliveries WHERE status IN ('delivered','failed') AND created_at < now() - interval '30 days'` (payloads are PHI-free, so this is hygiene, not compliance).

### 5.5 Circuit breaker

Per-endpoint `consecutive_failures` increments on every failed attempt to that endpoint and resets to 0 on any success — both as **atomic SQL** (`UPDATE webhook_endpoints SET consecutive_failures = consecutive_failures + 1 WHERE id=:id AND enabled RETURNING consecutive_failures`; an ORM read-modify-write would lose updates against a concurrent `PATCH enabled=true` reset and could double-fire the trip WARN — review L2). When the returned value reaches `WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD` (default 10): a second guarded UPDATE sets `enabled=false`, `disabled_reason='circuit_breaker'` (`WHERE id=:id AND enabled RETURNING id`, so the WARN log + `usan_webhook_endpoints_auto_disabled_total` increment fire exactly once; log carries `endpoint_id` only — never the URL). Pending rows stop being claimed (the join), so a dead receiver costs at most a handful of POSTs, not unbounded hammering. The breaker trip has its **own provisioned alert rule** (§9) — review found that a tripped breaker silently *prevents* rows from ever reaching `outcome="failed"`, so the delivery-failure alert alone can be permanently muted by the breaker. Recovery is explicitly operator-driven: `PATCH enabled=true` resets the counter and clears the reason; queued rows resume (until the 7-day expiry). Operator-set `enabled=false` keeps `disabled_reason` NULL so the two states are distinguishable in `GET`.

## 6. Event catalog — exact payload schemas

### 6.1 Envelope

Every payload (stored in `webhook_deliveries.payload`, identical across fan-out rows for one occurrence) is:

```json
{
  "event": "<event type>",
  "occurred_at": "<ISO-8601 UTC, the enqueuing transaction's timestamp>",
  "data": { ... }
}
```

At send time the serializer injects a top-level `"delivery_id"` (the `webhook_deliveries.id` of that row) into the body **before signing**, so the receiver's dedupe key is covered by the HMAC (review fix — the draft carried it only in an unsigned header). The stored payload omits it (the row id is already the `id` field of the deliveries-list response).

Payloads are constructed **only** by the typed Pydantic builders in `webhook_events.py` — an allowlist by construction: a field that isn't on the model cannot leak. Field rules (from the PHI audit of `Call`): ids are UUIDs; `status`/`direction`/`severity`/`origin.source` are bounded enums. `origin` is derived via `parse_origin` from the **chain root's** `idempotency_key`: retry children carry no key of their own (`schedule_retry` creates them without one), so the builder walks `parent_call_id` to the root (bounded, indexed single-row probes — the same walk bound as `schedule_retry`'s batch guard) and parses the root's key; `origin` therefore describes the chain's origin on **every** attempt, fixing the draft where attempts ≥ 2 would have carried `origin: null` while the spec promised chain tracking. `origin` is `null` only for operator one-off and inbound calls.

**Excluded everywhere, deliberately:** `end_reason` (conditionally LLM/caller free text — the codebase already refuses to log or label it, tools.py:206–213), flag `reason` **and `category`** (§6.4), callback `requested_time_text`/`notes`, `dynamic_vars`, `error` JSONB, names, phone numbers, `livekit_room`/`sip_call_id`/`recording_uri`/`egress_id`, batch `name` (PHI-free only "by convention", models.py:475–476 — convention is not enforcement), raw `idempotency_key` (operator free text — only the parsed bounded `origin` egresses), and `elder_id` on `flag.created` specifically (§6.4, §8.3).

### 6.2 `call.started` — transition into `in_progress`

Enqueued in `mark_answered` (now a guarded transition, §2.1) and `create_inbound_call`.

```json
{
  "event": "call.started",
  "occurred_at": "2026-06-10T17:04:05.123Z",
  "data": {
    "call_id": "uuid",
    "elder_id": "uuid | null",
    "direction": "outbound | inbound",
    "attempt": 1,
    "parent_call_id": "uuid | null",
    "origin": {"source": "schedule | batch", "id": "uuid", "ordinal": 3},
    "answered_at": "ISO-8601"
  }
}
```
(`origin` is `null` for operator-initiated and inbound calls; on retry attempts it is the **chain root's** origin (§6.1). `elder_id` is `null` for unknown inbound callers.)

### 6.3 `call.completed` — every terminal transition (per attempt)

```json
{
  "event": "call.completed",
  "occurred_at": "...",
  "data": {
    "call_id": "uuid",
    "elder_id": "uuid | null",
    "direction": "outbound | inbound",
    "status": "completed | voicemail_left | no_answer | busy | failed | dnc_blocked | cancelled",
    "attempt": 2,
    "parent_call_id": "uuid | null",
    "origin": {"source": "schedule | batch", "id": "uuid", "ordinal": 3},
    "created_at": "ISO-8601",
    "answered_at": "ISO-8601 | null",
    "ended_at": "ISO-8601 | null",
    "duration_seconds": 184
  }
}
```
`duration_seconds`/`answered_at`/`ended_at` are `null` where the lifecycle never reached them (e.g. `dnc_blocked` at birth). `origin` on attempt ≥ 2 is the chain root's origin (§6.1). No `recording_status` — recording finalizes after the terminal commit and would be a lie at enqueue time. No `end_reason` (§6.1).

### 6.4 `flag.created`

```json
{
  "event": "flag.created",
  "occurred_at": "...",
  "data": {
    "flag_id": 42,
    "call_id": "uuid",
    "severity": "routine | urgent",
    "created_at": "ISO-8601"
  }
}
```

**Deliberately reduced from the original brief (review HIGH — flagged in the header).** The brief specified `severity` + `category` + `elder_id`; review showed that the health-domain `category` enum (`medical | emotional | medication | safety | other`) keyed to the stable production `elder_id` is individually-identifiable health information for a recipient that also holds API access — which broke the "PHI-free tier, receivers need no BAA" claim for the one event whose purpose is paging. Resolution: keep `severity` (the paging use case — `urgent` pages, `routine` doesn't — survives intact), drop `category` and `elder_id` from the egressed payload. The care system pages on `urgent`, then resolves who/what via the authenticated API (`call_id` → call → elder; flag detail by `flag_id`). `reason` (free text ≤2000) was always excluded — same rule that keeps it out of logs and metric labels today. Restoring `category`/`elder_id` is the first candidate for the Phase C gated PHI tier (open Q5).

### 6.5 `callback.created`

```json
{
  "event": "callback.created",
  "occurred_at": "...",
  "data": {
    "callback_id": 17,
    "call_id": "uuid",
    "elder_id": "uuid",
    "requested_at": "ISO-8601 | null",
    "created_at": "ISO-8601"
  }
}
```
`requested_at` is the parsed timestamp only; `requested_time_text` and `notes` are excluded. (`elder_id` stays here: a callback request carries no health content — the PHI line drawn in §6.4 is the *health-information × person-identifier* pairing.)

### 6.6 `batch.completed`

**Single emission point: phase-6 `_complete_drained_batches`** (review fix — the draft's cancel-endpoint emission was unconstructible: at cancel time `completed_at` is NULL, the histogram is non-final because in-flight chains settle afterwards, and the idempotent endpoint would double-emit on re-cancel). Phase 6 stamps `completed_at` for both drained-`completed` and drained-`cancelled` batches; the existing `stamped = [(batch.id, batch.status)…]` list drives one event per stamped batch, where `completed_at` is real and `final_status_histogram` is settled. Consequence, stated for consumers: a cancelled batch's event arrives when its in-flight work **settles** (drains), not at cancel-API time — the cancel API's 200 response is the immediate acknowledgment (an immediate `batch.cancelled` event is open Q11). `target_count` is not a `CallBatch` column; the builder computes it with the same aggregate used for the histogram (call_batches.py:151) at enqueue time.

```json
{
  "event": "batch.completed",
  "occurred_at": "...",
  "data": {
    "batch_id": "uuid",
    "status": "completed | cancelled",
    "target_count": 250,
    "final_status_histogram": {"completed": 230, "no_answer": 12, "failed": 5, "cancelled": 3},
    "completed_at": "ISO-8601"
  }
}
```
Histogram keys are the bounded call statuses. Batch `name` excluded (§6.1).

### 6.7 `ping` (test deliveries only; not subscribable)

```json
{"event": "ping", "occurred_at": "...", "data": {"endpoint_id": "uuid"}}
```

## 7. Signing & receiver verification

**Wire format** (Retell/Stripe-style):

```
POST <endpoint url>
Content-Type: application/json
User-Agent: usan-voice-engine-webhooks/1.0
X-Usan-Event: call.completed
X-Usan-Delivery-Id: <webhook_deliveries.id>
X-Usan-Signature: v=<unix_ms>,d=<hex digest>
```

- `v` = sender's Unix epoch **milliseconds** at send time — fresh per attempt, so retries re-sign.
- `d` = `hex(HMAC_SHA256(secret, f"{v}." + raw_body))` where `raw_body` is the exact bytes of the request body.
- **Canonical JSON, sign-what-you-send:** JSONB round-trips do not preserve key order or whitespace, so the body is serialized at send time as `json.dumps(body, sort_keys=True, separators=(",", ":"))` (UTF-8) — where `body` = stored payload + injected `delivery_id` (§6.1) — and the signature is computed over those exact bytes. The signature is always valid for the body actually received; receivers must verify against the raw bytes, **not** a re-serialized parse.
- **Headers are routing convenience only and are NOT covered by the HMAC** (review fix): after verifying the signature, receivers must take `event` and `delivery_id` from the **signed body**, never trust the unsigned `X-Usan-Event`/`X-Usan-Delivery-Id` headers for dedupe or dispatch decisions.
- Secret: 64 hex chars (`secrets.token_hex(32)`); never logged, never in error columns, never re-readable via API.

**Documented receiver verification** (ships in the endpoint-create response docs / README section):

```python
import hashlib, hmac, time

def verify_usan_signature(secret: str, header: str, raw_body: bytes,
                          tolerance_s: int = 300) -> bool:
    parts = dict(kv.split("=", 1) for kv in header.split(","))
    ts_ms = int(parts["v"])
    if abs(time.time() * 1000 - ts_ms) > tolerance_s * 1000:
        return False  # replay window exceeded
    expected = hmac.new(
        secret.encode(), f"{ts_ms}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts["d"])
```

Receivers should: verify before parsing; use constant-time compare; enforce the **5-minute tolerance** (replay rejection — the same posture our inbound `verify_livekit_webhook` takes, mapping replay and forgery to the same rejection); dedupe on the body `delivery_id` (at-least-once delivery; redeliveries intentionally reuse the id); respond 2xx fast and process async (we time out at 10 s and a slow handler burns ladder attempts).

**Ordering is NOT guaranteed** (review fix — the draft was silent): each delivery row retries on its own ladder, so `call.completed` can arrive before a retried `call.started` for the same call; events for one entity can arrive at different endpoints at different times; trust `occurred_at`, not arrival order, and tolerate out-of-order transitions. As defense-in-depth beyond `delivery_id` dedupe, the payloads are designed so semantic idempotency keys exist for every event: `(event, call_id)` (a call id is unique per attempt and each call reaches `started`/`completed` at most once after the §2.1 hardening), `flag_id`, `callback_id`, `(batch_id, status)`.

## 8. Security

### 8.1 SSRF — registration-time validation (layer 1)

The VM hosts a GCP metadata server at `169.254.169.254`; operator-configured egress URLs are a textbook SSRF vector. `ssrf_guard.validate_webhook_url` runs in the Pydantic schema validator on create **and** on every `PATCH url`:

- Scheme `https://` only, **case-folded** before checking (extends the `_https_scheme` settings-validator precedent with the same PHI rationale).
- No userinfo (`user:pass@`), no fragment; max length 2048.
- Hostname **normalized before all checks**: lowercased, trailing dot stripped (review fix — `metadata.google.internal.` must not slip past a suffix match).
- Host must be a DNS name — IPv4 and IPv6 literals rejected outright, **including decoy literal forms** (review fix): any all-numeric dotted host, `0x`-prefixed hex (`0x7f000001`, `0x7f.0.0.1`), leading-zero octal (`0177.0.0.1`), and bare-decimal (`2130706433`) forms are rejected at registration even though `ipaddress` does not parse them (they resolve via `inet_aton` semantics at connect time; layer 2 also catches them — both layers are tested, §10.4).
- Hostname denylist (applied to the normalized name): `localhost`, `*.localhost`, `*.local`, `*.internal` (covers `metadata.google.internal`), `*.home.arpa`, single-label hosts.
- Port: absent (=443), 443, or 8443 only (8443 retained per the original brief for operator-gateway convention; whether to narrow to 443-only is open Q10 — review noted it widens the rebind surface).

### 8.2 SSRF — delivery-time resolver gate (layer 2)

Registration-time checks are insufficient against DNS pointing at private space (or being re-pointed later). Before **every** POST, `resolve_public_or_raise(host)` resolves all A/AAAA records (`getaddrinfo`) and rejects the delivery unless the resolution returned **at least one address AND every** resolved address is public:

- **Fail-closed on empty resolution** (review fix): zero addresses → reject. A bare `all(...)` over an empty list is vacuously true; the implementation must check non-emptiness first, and §10.4 pins it.
- Each address is unwrapped first: `ip = ipaddress.ip_address(addr); ip = ip.ipv4_mapped or ip` (explicit IPv4-mapped-IPv6 unwrap — `::ffff:169.254.169.254` and `::ffff:10.0.0.1` must be judged as their IPv4 selves; py3.14 evaluates `is_global` correctly on mapped forms, but the explicit unwrap + test pins the behavior against any future runtime change).
- Then require `ip.is_global == True` — this single predicate covers RFC1918, 127/8, 169.254.0.0/16 (the metadata server), 100.64/10, 0.0.0.0, `::1`, `fc00::/7`, `fe80::/10`, and reserved space.

Rejection is recorded as a failed attempt (`last_error='SsrfBlocked'`; outcome `ssrf_blocked` on non-terminal attempts, `failed` on the terminal one — §5.3) and feeds the circuit breaker.

**TOCTOU residual, stated honestly and in full breadth (review fix — the draft framed it as metadata-only):** httpx has no first-class resolve-then-connect IP pinning, so this phase is check-then-connect — a malicious DNS server could rebind between our resolve and httpx's. The metadata server specifically is additionally defended by its required `Metadata-Flavor: Google` request header, which our client never sends — but that defense is metadata-specific: a successful rebind could still land a POST on *other* internal TLS services listening on 443/8443 (our own Caddy/API, internal dashboards). Mitigations that bound the harm: (1) both-layer checks run on **every** attempt; (2) `follow_redirects=False` — a 3xx is a failure, never followed, closing the redirect-to-internal classic; (3) ports restricted to 443/8443; (4) the request body is PHI-free by construction (§6), so even a fully successful rebind exfiltrates only opaque ids and bounded enums; (5) the POST carries no credentials of ours (the HMAC secret is per-endpoint and signs, not authenticates-to, anything internal). Closing the residual properly — a custom `httpx.AsyncHTTPTransport` that connects to the vetted IP while sending SNI/Host for the original name — is open Q2, **elevated by review**: do it before enabling delivery against untrusted-DNS receivers at scale.

### 8.3 PHI posture & secret handling

- Payloads are PHI-free by construction (§6.1 allowlist builders): no free text, no direct identifiers, no person-linked health categories. This — payload minimization, not receiver trust — is the BAA argument: the webhook channel egresses nothing usable as PHI, so receivers need no BAA, and a misconfigured/compromised endpoint leaks opaque ids and bounded enums only. The one place where this claim required a design change rather than an assertion is `flag.created` (§6.4): with `category`+`elder_id` it would NOT have been defensibly PHI-free, so those fields do not ship. The richer tier waits for Phase C compliance mode with an explicit gate (open Q5).
- `elder_id` appears in `call.*`/`callback.created` payloads (opaque UUID, no health content attached, useless without authenticated API access) but is **excluded from delivery logs**, per the house rule that treats elder linkage as PHI-adjacent in logs (tools.py:268 precedent) — and excluded from `flag.created` payloads entirely (§6.4).
- Endpoint URLs may carry operator query-string tokens → logs carry `endpoint_id` only, never the URL; `last_error` carries exception type names only, never `str(exc)`; response bodies are never stored (status code only).
- Secrets: server-generated, shown once at create, plaintext-at-rest in Cloud SQL (encrypted at rest), absent from all reads/logs/audit payloads. Rotation = delete + recreate this phase (open Q1).
- **`grafana_ro` must never be granted on `webhook_endpoints`** (review note, made load-bearing): migration 0009 uses explicit per-table `GRANT SELECT` with no `ALTER DEFAULT PRIVILEGES`, so the secret column is not auto-exposed to the reporting role — keep it that way; any future reporting need gets a view that excludes `secret`.

### 8.4 Abuse limits

- `/v1/webhook-endpoints` and `/v1/webhook-deliveries` prefixes added to `ratelimit._is_operator_route` — covered by the per-process fixed window (single-replica assumption already documented there). Covered by `test_app_security.py` additions.
- Hard cap: **10 endpoints** (422 beyond) — bounds fan-out write amplification in hot call mutators and bounds breaker-bypass-by-many-endpoints. App-level count; the concurrent-create 11th-row race is accepted and stated (§3.1).
- Redeliver: 409 if already pending; **429 when the endpoint has ≥100 pending deliveries** — bounds re-arm storms from a leaked operator key (§4).
- `description` ≤500 chars; `events` deduplicated, sorted, subset of the closed enum (schema + DB CHECK); deliveries list `limit` ≤100.
- Enqueue fan-out targets enabled endpoints only; a disabled endpoint accrues nothing new, and its stranded pending rows expire after 7 days (§5.4).

## 9. Observability

- **`usan_webhook_deliveries_total{event, outcome}`** — counter in `observability/custom_metrics.py` as `WEBHOOK_DELIVERIES_TOTAL`. Both labels bounded: `event` ∈ the 5 events + `ping`; `outcome` ∈ `delivered | retry_scheduled | failed | ssrf_blocked`, with the terminal-attempt-always-`failed` rule pinned in §5.3. Named to avoid colliding with the existing inbound `WEBHOOKS_TOTAL`. Increment-after-commit everywhere.
- **`usan_webhook_endpoints_auto_disabled_total`** — no labels; exactly one increment per breaker trip (guarded-UPDATE semantics, §5.5), alongside the WARN log (`component=webhook_delivery`, `endpoint_id` only).
- **`usan_webhook_pending_deliveries`** — Gauge (house precedent: `IN_FLIGHT_CALLS`/`DIAL_SLOTS_FREE`), set each poller cycle to the count of `status='pending'` rows; no labels (endpoint-id labels would be unbounded over time). This is the backlog-visibility instrument review found missing: flag-off backlogs and breaker-stranded rows are now observable without SQL; per-endpoint pending counts surface in `GET /v1/webhook-endpoints` (§4).
- **Alerts as code — two rules** (review fix: the breaker can permanently mute the failure alert, because disabled endpoints' rows are never claimed and so never reach `outcome="failed"`; the trip itself must page):
  - `usan-webhook-delivery-failed`: `increase(usan_webhook_deliveries_total{outcome="failed"}[30m]) > 0`, severity `warning`.
  - `usan-webhook-endpoint-auto-disabled`: `increase(usan_webhook_endpoints_auto_disabled_total[30m]) > 0`, severity `warning`.
  Both in `infra/grafana/provisioning/alerting/usan_alerts.yml`, cloning `usan-sms-delivery-failed`: `noDataState: OK` / `execErrState: Alerting` (pinned with the standing rationale), `usan-operator` contact point. Both uids added to `scripts/tests/test_alerting_provisioning.py::test_alert_rules_present` (the nodata/execerr loop covers them automatically), plus the env-plumbing test asserting file + uid presence.
- **Queryability:** `GET /v1/webhook-endpoints/{id}/deliveries` is the operator debugging surface — status, attempts, response codes, error type names, last-attempt timestamp (`updated_at`), and the (PHI-free) payload itself.
- Logs: one INFO per outcome (`delivery_id`, `endpoint_id`, `event`, `outcome`, `response_code`), WARN on breaker trips, per-cycle try/except ERROR with `type(exc).__name__` only.

## 10. Testing strategy (TDD — tests written first per task)

1. **Migration contract** (`test_webhook_migration.py`, cloning `test_ops_queue_migration.py`): information_schema introspection of both tables, CHECKs, partial index; subprocess `alembic downgrade 0013` → seed → `upgrade head` roundtrip (always upgrade back to head, per 6b90cbe). Both tables added to the conftest TRUNCATE list.
2. **Models** (`test_webhook_models.py`): defaults, CHECK violations (bad status, bad event, empty/unknown events array), cascade delete.
3. **Settings** (`test_settings_webhooks.py`): flag default false, interval/timeout/threshold bounds, `get_settings.cache_clear()` discipline.
4. **SSRF rejection matrix** — registration: `http://`, `HTTPS://` (case-fold accept), IPv4/IPv6 literals, decoy literals (`https://2130706433/`, `https://0x7f000001/`, `https://0x7f.0.0.1/`, `https://0177.0.0.1/`), `localhost`, `foo.internal`, `metadata.google.internal`, `metadata.google.internal.` (trailing dot), `METADATA.GOOGLE.INTERNAL` (case), `*.local`, userinfo, port 8080, oversize URL → 422; valid `https://hooks.example.com:8443/path?x=y` accepted. Delivery-time: monkeypatched `getaddrinfo` returning `169.254.169.254`, `10.0.0.5`, `::1`, `fd00::1`, `::ffff:169.254.169.254`, `::ffff:10.0.0.1` (IPv4-mapped unwrap), mixed public+private (rejected — *every* address must be global), **empty result (rejected — fail-closed)**, all-public (accepted); rejection marks the attempt failed with `last_error='SsrfBlocked'`, increments the breaker, and the **terminal** SSRF attempt emits `outcome="failed"` (alert-honesty rule, §5.3).
5. **Signature vector**: fixed secret, fixed `v`, fixed canonical body → pinned hex digest; round-trip through the documented `verify_usan_signature` snippet, including the 5-minute tolerance boundary and a tampered-body rejection; **`delivery_id` present in the signed body** and matching the row id.
6. **Canonical serialization**: signed bytes == sent bytes after a JSONB round-trip (key reordering must not break verification).
7. **Outbox-in-same-txn + genuine concurrency** (review fix — the A1 race tests are sequential and would pass without the hardening): each §2.1 site enqueues fan-out rows visible only after the caller's commit; rollback leaves zero rows. **Two-concurrent-sessions tests** (separate engine connections): `outcome(voicemail)` vs `room_finished(completed)` produce exactly one terminal transition and one `call.completed`; `end_call` vs `room_finished` ditto; stale `mark_dial_failure` after `reclaim_stuck_dialing` re-queue is a no-op (returns None, zero enqueues); `mark_answered` after terminal is a no-op (no zombie resurrection, no enqueue); `set_status` terminal→terminal enqueues zero; `cancel_queued_tips` emits one event per returned id; DNC-at-birth paths emit; **batch re-cancel emits nothing** (no cancel-endpoint enqueue exists) and phase-6 stamps each drained batch exactly once → exactly one `batch.completed` per batch, both statuses.
8. **Fan-out rules**: only enabled endpoints subscribed to the event get rows; zero endpoints → zero rows; `ping` deliverable without subscription; events list deduplicated at the schema layer.
9. **Origin root-walk**: retry child (attempt 2, no idempotency_key) carries the root's parsed origin; operator one-off and inbound carry `origin: null`.
10. **Delivery worker**: ladder schedule (+1 m/+5 m/+30 m, failed at attempt 4); claim respects `enabled` join and `attempts < 4`; per-endpoint grouping delivers concurrently and preserves intra-endpoint order; mid-cycle breaker trip stops the rest of that endpoint's group; crash-after-claim row re-offers at next rung; crash-residue sweep (COALESCE keeps a genuine last_error); **7-day pending expiry** (`last_error='expired'`); per-row outcome commits; guarded `mark_attempt_failed`/`mark_delivered`/redeliver UPDATEs; non-2xx/3xx/timeout all count as failures; redirects not followed; 30-day prune; housekeeping runs with the flag **off** (hourly cadence).
11. **Circuit breaker**: atomic SQL increment (concurrent failure + PATCH-reset does not lose updates or double-WARN); trips at threshold exactly once (enabled=false, `disabled_reason='circuit_breaker'`, WARN, metric); success resets the count; PATCH re-enable resets and resumes queued rows.
12. **Lifespan** (extend `tests/test_lifespan_poller.py`): webhook poller **always** starts; with `WEBHOOK_DELIVERY_ENABLED=false` no claims/POSTs occur but housekeeping runs.
13. **API**: CRUD happy paths; secret present in 201 and absent from every other response; 10-endpoint cap; sentinel-actor audit rows in same commit per mutation; `/test` 409 when delivery disabled; redeliver guarded-reset semantics, 409 on pending row, 409 on disabled endpoint, 429 over the pending cap; deliveries pagination/filters + `updated_at` present; `pending_deliveries` in endpoint list; rate-limit allowlist coverage in `test_app_security.py`.
14. **Env plumbing contract** (`test_infra_webhooks_env.py`, cloning `test_infra_scheduler_env.py`): keys in compose api environment, dev compose defaults `WEBHOOK_DELIVERY_ENABLED:-true` (scheduler precedent — otherwise `/test` 409s in every dev stack), commented `# KEY=` in `.env.example` with the inbound-vs-outbound `WEBHOOK_*` comment block, pinned `false` in `.env.prod.example`, `${WEBHOOK_DELIVERY_ENABLED:-false}` re-pin in `docker-compose.prod.yml`; **both** alert-rule uids present.
15. **mypy + ruff** locally before push (CI runs mypy even though CLAUDE.md omits it).

## 11. Rollout & ops

1. **Ship inert.** `WEBHOOK_DELIVERY_ENABLED=false` pinned in `.env.prod.example` and re-pinned `${WEBHOOK_DELIVERY_ENABLED:-false}` in the prod compose overlay; dev compose defaults `true` (scheduler precedent) so `/test` works in every dev stack. Inertness is double-layered: with no registered endpoints, enqueue fan-out inserts zero rows (zero hot-path cost); with the flag off, nothing egresses even if endpoints exist — housekeeping still runs (§5.1) so a flag-off backlog is swept, expired, and visible on the gauge.
2. **Deploy reality** (standing constraints): merging to main changes nothing on the VM — app code + compose ship on a `v*` tag; the new env keys require a VM `.env` refresh (reboot or surgical IAP-SSH edit of `/opt/usan/infra/.env`) **before** the tag deploy; Grafana loads provisioned alert rules at startup only, so the **two** new rules need a Grafana container restart.
3. **Enable sequence:** refresh `.env` (flag still false) → tag deploy (runs migration `0014` automatically) → set `WEBHOOK_DELIVERY_ENABLED=true` + restart api → `POST /v1/webhook-endpoints` (capture the secret once) → `POST .../test` → confirm the ping row reaches `delivered` via `GET .../deliveries` → subscribe real events.
4. **Backlog semantics:** registering endpoints while the flag is off accumulates pending rows — now **bounded**: the 7-day pending expiry (§5.4) caps both volume and staleness, and the backlog is visible on `usan_webhook_pending_deliveries` + per-endpoint counts in the list endpoint. On enable, the surviving backlog drains through the normal ladder (receivers see stale `occurred_at` ≤ 7 days and should trust it over arrival time). Operationally: still prefer enabling the flag before or together with endpoint registration.
5. **Breaker runbook:** `usan-webhook-endpoint-auto-disabled` alert (or the WARN log) → inspect `GET /v1/webhook-endpoints` for `disabled_reason='circuit_breaker'` and the endpoint's recent deliveries (`updated_at` = last attempt) → fix the receiver → `PATCH enabled=true` (resets the counter, resumes queued rows that haven't expired).
6. Single-replica delivery documented (same assumption as the rate limiter); the SKIP LOCKED claim makes a future second replica safe, not load-bearing today.
7. **Stacked-branch ritual** (review fix — A3 is third in an unmerged stack behind PR #55/A1 and PR #56/A2, and instruments functions A1 introduced): after **each** predecessor squash-merges, run `git rebase --onto origin/main <prev-plan-tip>` (the house plan-PR workflow), then re-run the §10.1 migration roundtrip (a review-cycle renumber or an unrelated `0014` landing on main breaks `down_revision="0013"`) and re-verify the §2.1 enqueue-site inventory against the rebased tree (line anchors and A1-introduced function shapes may have churned in PR review). Then this ships as its own squash-merged PR.

## 12. Open questions

1. **Secret rotation** — add `POST /v1/webhook-endpoints/{id}/rotate-secret` (return new secret once, optional dual-validity overlap window) vs the current delete+recreate? Stripe-style dual-secret overlap is the likely answer when a real consumer exists.
2. **Resolve-then-connect IP pinning** *(elevated by security review)* — implement via a custom `httpx.AsyncHTTPTransport` (connect to the vetted IP, send SNI/Host for the original name) to close the DNS-rebinding TOCTOU residual (§8.2). Recommended before enabling delivery to receivers whose DNS we don't control at scale; deferred this phase per the original brief's "else document" latitude, with the residual bounded by PHI-free payloads + no-redirects + port restriction.
3. **Event-driven nudge** — spawn a delivery flush after enqueueing commits (BackgroundTasks where a request context exists, a direct task in pollers) to cut latency below the 10 s poll interval, keeping the poller as the at-least-once backstop?
4. **`call.chain_settled`** — emit from the phase-1 finalizer (tip terminal AND no child) so consumers don't reconstruct retry chains? Needs an answer for operator one-off calls, which have no finalizer today.
5. **PHI payload tier (Phase C)** — what gate (per-endpoint BAA attestation flag? mTLS? allowlisted receiver domains?) would justify restoring `category` + `elder_id` to `flag.created` (the fields removed by this review — §6.4), and beyond that names/reasons/transcript pointers?
6. **Workspace scoping** — Phase B `org_id` on `webhook_endpoints` and per-org event filtering.
7. **Admin-UI surface** — endpoint CRUD + delivery log viewer on the `Operate` sidebar group (operator API stays the source of truth, mirroring A1→A2).
8. **Static receiver auth header** — some operator gateways require a fixed bearer/header in addition to signature verification; per-endpoint custom header support is deferred until a consumer asks (it is also a secret-storage expansion).
9. **Bulk redeliver** — `POST /v1/webhook-endpoints/{id}/redeliver-failed` after a long receiver outage, vs scripting the per-delivery endpoint (now also interacting with the 100-pending redeliver cap).
10. **Drop port 8443?** — security review flagged it as widening the rebind surface "for no stated reason"; the original brief pins 443/8443. Kept per the brief this phase; revisit if no consumer actually uses 8443 (unresolvable here without consumer input — pre-seeded decision vs review finding).
11. **Immediate `batch.cancelled` ack event** — `batch.completed{status=cancelled}` now fires at drain-settlement (§6.6), which is correct but late; should a separate immediate acknowledgment event exist, or is the cancel API's synchronous response sufficient?
