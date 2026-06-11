# Batch & Scheduled Calling — recurring wellness schedules + one-off batch campaigns (design)

**Date:** 2026-06-10
**Status:** Final — adversarial review applied (security/TCPA, correctness, ops lenses); findings disposition in Appendix A
**Predecessors:** Retry orchestrator (`apps/api/src/usan_api/retry_orchestrator.py`, merged); Admin-UI Phase 3 tools (PR #54, squash pending); follow-up/callback/SMS tables (migration `0011`)
**Related specs:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md`, `docs/superpowers/specs/2026-06-09-admin-ui-phase3-tools-design.md`

---

## 1. Goals & non-goals

### 1.1 Goals

Give the platform an **in-repo owner for outbound call initiation at scale**, closing the documented cutover blocker that the daily wellness call trigger is "external/unowned" while `POST /v1/calls` remains the only entry point. Two entities:

- **CallSchedule** — a recurring daily wellness call per elder: a local-time calling window in the elder's IANA timezone, a days-of-week mask, an enabled flag, optional `profile_override` and `dynamic_vars`.
- **CallBatch** — a one-off campaign: a list of targets with per-target `dynamic_vars`/`profile_override`, an optional `trigger_at`, an optional calling time window, and a per-batch materialization throttle.

Both entities **materialize ordinary `Call` rows through the existing machinery** — idempotency keys, DNC gates, LiveKit dispatch, the retry ladder, stuck-DIALING reclaim, and crash-marking are all reused, not reimplemented. Deterministic idempotency keys (`sched:{schedule_id}:{local_date}`, `batch:{batch_id}:{target_index}`) make re-polls and crash recovery structurally unable to double-call.

RetellAI parity reference (their `create-batch-call`): tasks[] with per-task dynamic vars + agent override, `trigger_timestamp`, `reserved_concurrency`, `call_time_window` (tz + day-of-week), statuses Draft→Planned→Ongoing→Sent. Our mapping: `scheduled`≈Planned, `running`≈Ongoing, `completed`≈Sent. Retell has **no built-in retry policy** — our ladder applies unchanged to every materialized call and is the deliberate differentiator (§6).

### 1.2 Rejected alternatives

- **APScheduler / external cron** — APScheduler was explicitly rejected earlier in this project (Telnyx-AMD/poller decision); an external cron reintroduces the unowned-trigger problem. The scheduler is an **in-process DB poller in `apps/api`**, the third poller alongside retry and retention, using the identical `FOR UPDATE SKIP LOCKED` claim pattern.
- **A second dial path** — batch calls do not get their own dispatch code. Materialized rows are `QUEUED` with `scheduled_at` set, so the **existing retry poller** claims them via `claim_due_retries` and dials via `dispatch_and_dial` (DNC re-check, trunk handling, classification, retry scheduling all included). This coupling is embraced deliberately, not accidentally (§2.2). The dial path itself gains three small, load-bearing hardenings this phase (§2.3).
- **CSV upload** — out; the operator sends JSON.
- **Per-call kill/teardown on batch cancel** — out; cancel stops future dials only (§5.6).

### 1.3 Non-goals (this phase)

- Admin-UI surface for schedules/batches (Phase A2; this phase is operator-API only).
- CSV/spreadsheet target ingestion.
- Grafana **panels** for the new metrics (counters land alert-ready; panels later). One provisioned-as-code **alert rule** *is* in scope (§7) — the concurrency gate makes `dial_slots_free == 0` load-bearing, and alerts-as-code already exist in `infra/`.
- Mid-call teardown of in-flight calls on cancellation.
- Multiple schedule occurrences per elder per day (exactly one materialization per elder-local date).
- Pacing knobs beyond the concurrency cap (calls-per-minute shaping).
- Multi-replica scale-out (single-replica documented, same as trunk auto-provisioning).

## 2. Architecture

```
operator (OPERATOR_API_KEY bearer, rate-limited — §4)
   │
   ├─ POST/GET/PATCH/DELETE /v1/schedules        ┐ routers/schedules.py
   ├─ POST/GET /v1/batches, /cancel              ┘ routers/batches.py
   ▼
call_schedules / call_batches / call_batch_targets        (migration 0012)
   ▼
schedule_orchestrator.run_poller          ← NEW, 3rd lifespan poller (env-gated)
   │  materializes Call rows: QUEUED + scheduled_at + idempotency_key
   ▼
retry_orchestrator.poll_once              ← EXISTING, gains the concurrency gate
   │  claim_due_retries (FOR UPDATE SKIP LOCKED, limit = min(batch, free slots))
   ▼
livekit_dispatch.dispatch_and_dial        ← EXISTING (DNC re-check + NEW quiet-hours
                                            re-check + elder-missing fix; classify, retry)
```

### 2.1 Component placement

Everything lives in `apps/api` (no `services/agent` changes; the agent sees ordinary outbound dispatches). New files:

| File | Role |
|---|---|
| `src/usan_api/schedule_windows.py` | Pure window/day-mask/next-occurrence math (zoneinfo only) |
| `src/usan_api/schedule_orchestrator.py` | The poller (mirrors `retry_orchestrator.py` structure) |
| `src/usan_api/repositories/call_schedules.py`, `call_batches.py` | Async module functions, flush+refresh, never commit |
| `src/usan_api/schemas/schedule.py`, `batch.py` | Pydantic request/response pairs + caps |
| `src/usan_api/routers/schedules.py`, `batches.py` | Operator-plane routers |
| `migrations/versions/0012_batch_scheduled_calling.py` | Tables + indexes (raw `op.execute`, house style) |

Touched existing files (small, surgical): `retry_orchestrator.py` (concurrency gate + gauges), `livekit_dispatch.py` (quiet-hours re-check, elder-missing guard fix), `repositories/calls.py` (`schedule_retry`: `profile_override` propagation + batch-cancellation awareness), `ratelimit.py` (`_is_operator_route` prefixes), `retention.py` (settled-target scrub), `schemas/call.py` (reserved key-prefix rejection), `routers/calls.py` (derived `origin` in `CallResponse`), `main.py` (3rd poller wiring), `settings.py` (eight keys).

### 2.2 Forward-compat invariants

1. **`calls` stays the single source of truth for call state.** The batch engine writes call status exactly once: the guarded `queued → cancelled` transition on batch cancel (§5.6). `CANCELLED` — previously an enum value with no writer — gets its first writer here. The dial-path changes (§2.3) are dial-path code, not batch-engine writes.
2. **`scheduled_at IS NOT NULL` is redefined** from "retry child" to "**poller-owned row**" (retry child *or* schedule/batch root). The `reclaim_stuck_dialing` docstring and the `idx_calls_due_retries` comment are updated to say so; no predicate changes are needed — the existing claim/reclaim predicates already do the right thing for these rows.
3. **Deterministic idempotency keys are the cross-replica/crash guard**, exactly as `uq_calls_parent_call_id` is for retries: correctness by schema, not by code. The `sched:`/`batch:` prefixes become a **reserved namespace**: `CreateCallRequest.idempotency_key` rejects them with 422, and the materializer's replay path verifies ownership before adopting a row (§5.3) — a squatted or colliding key can never silently substitute a foreign call.
4. Retry children continue to carry **no** idempotency key (unchanged).

### 2.3 Dial-path hardenings shipped with this phase

Three pre-existing dial-path gaps become load-bearing once calls dial autonomously at scale; they are fixed here, each with tests:

1. **Quiet-hours re-check at dial time** (TCPA). Today quiet hours are enforced only when `scheduled_at` is *written*; the dial happens whenever `claim_due_retries` gets to the row — and the new concurrency gate deliberately makes claims wait. A call clamped to 20:59 that is claimed at 21:20 elder-local would dial inside statutory quiet hours. Fix: `dispatch_and_dial` re-computes `quiet_hours.next_allowed(now, elder.timezone)` after loading the elder; if `> now`, it flips the row back to `QUEUED` with `scheduled_at = next_allowed`, increments `usan_dial_requeued_total{reason="quiet_hours"}`, logs WARNING, and returns — **never dials on a stale clamp**. A `ValueError` (invalid tz) marks the call FAILED fail-closed (and `schedule_retry` independently refuses invalid-tz children, so the chain settles). The push-to-next-morning edge for a schedule root is bounded by the per-elder daily cap (§5.3).
2. **Elder-deleted DIALING ping-pong.** `dispatch_and_dial`'s first guard (`call.elder_id is None or not call.livekit_room`) currently returns **without any status write**; since `calls.elder_id` is `ON DELETE SET NULL`, an elder deletion leaves the row DIALING forever — `reclaim_stuck_dialing` re-queues it, the poller re-claims it, an infinite loop that pins one in-flight slot and never settles its chain. Fix: that guard now marks the call `FAILED` with `end_reason="elder_missing"` (the existing `elder is None` branch below it is unreachable given the FK and is folded in).
3. **`schedule_retry` propagates `profile_override`.** Today the child copies only `dynamic_vars`, so attempts 2..n of an override-bearing call silently revert to the default profile — and `profile_override` **is live today** (the agent's `GET /v1/runtime/agent-config` resolves it at highest precedence, and SMS template resolution uses it). One-line fix + test (§6.1).

## 3. Data model

Migration `0012_batch_scheduled_calling.py` (`down_revision="0011"`), raw SQL via `op.execute`, `downgrade()` drops with `IF EXISTS` in reverse order. Models added to `db/models.py`; tables added to the conftest TRUNCATE list FK-children-first: `call_batch_targets, call_batches, call_schedules` prepended before the existing list.

### 3.1 `call_schedules`

```sql
CREATE TABLE call_schedules (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- CASCADE: a schedule is meaningless without its elder; calls.elder_id
    -- already SET NULLs independently, so history survives.
    elder_id               UUID NOT NULL UNIQUE REFERENCES elders(id) ON DELETE CASCADE,
    enabled                BOOLEAN NOT NULL DEFAULT true,
    window_start_local     TIME NOT NULL,            -- elder-local wall clock
    window_end_local       TIME NOT NULL,
    days_of_week           SMALLINT NOT NULL DEFAULT 127,  -- bit 0=Mon … bit 6=Sun
    dynamic_vars           JSONB NOT NULL DEFAULT '{}',    -- 8 KB cap (schema layer)
    profile_override       UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
    next_run_at            TIMESTAMPTZ NOT NULL,     -- computed in Python (zoneinfo)
    last_materialized_date DATE,                     -- elder-local date last fired
    last_result            TEXT,                     -- per-elder skip observability (§5.2)
    last_result_at         TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_call_schedules_window CHECK (window_start_local < window_end_local),
    CONSTRAINT ck_call_schedules_days   CHECK (days_of_week BETWEEN 1 AND 127),
    CONSTRAINT ck_call_schedules_result CHECK (last_result IS NULL OR last_result IN
        ('created','replayed','rescheduled','skipped_window','skipped_invalid_timezone',
         'skipped_daily_cap','dnc_blocked','key_conflict'))
);
-- The poller's exact claim predicate (idx_calls_due_retries precedent):
CREATE INDEX idx_call_schedules_due ON call_schedules (next_run_at) WHERE enabled;
```

One schedule per elder, enforced by `UNIQUE (elder_id)` — it is *the* daily wellness call. `enabled=false` pauses; `DELETE` removes. The schedule has no timezone column: the elder's `elders.timezone` (NOT NULL) is the single source of truth. Because both the schedule window and quiet hours are elder-local **wall clock**, their intersection is timezone-invariant — it is validated once at create/patch and can never become empty from a timezone edit. A timezone edit *can* make the stored `next_run_at` stale (it was computed under the old zone); §5.2 phase 3 handles both directions explicitly.

`last_result`/`last_result_at` answer the day-2 operator question "which elder missed today's call?" without grepping metrics: every materialization decision writes them, and `GET /v1/schedules?last_result=skipped_window` lists the misses (§4.1).

### 3.2 `call_batches`

```sql
CREATE TABLE call_batches (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,               -- operator label; PHI-free by convention, see §8
    idempotency_key     TEXT UNIQUE,                 -- optional create-replay guard
    payload_digest      TEXT NOT NULL,               -- sha256 of canonical payload (§4.2 replay)
    status              TEXT NOT NULL DEFAULT 'scheduled',
    trigger_at          TIMESTAMPTZ,                 -- NULL = next poll cycle
    window_start_local  TIME,                        -- optional per-elder-local window
    window_end_local    TIME,
    days_of_week        SMALLINT,                    -- NULL = any day
    max_concurrency     SMALLINT,                    -- materialization throttle, NOT a dial cap (§5.2)
    profile_override    UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,                 -- also stamped on drained cancelled batches
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_call_batches_status CHECK
        (status IN ('scheduled','running','completed','cancelled')),
    CONSTRAINT ck_call_batches_window CHECK (
        ((window_start_local IS NULL) = (window_end_local IS NULL))
        AND (window_start_local IS NULL OR window_start_local < window_end_local)),
    CONSTRAINT ck_call_batches_days CHECK
        (days_of_week IS NULL OR days_of_week BETWEEN 1 AND 127),
    CONSTRAINT ck_call_batches_maxconc CHECK
        (max_concurrency IS NULL OR max_concurrency >= 1)
);
CREATE INDEX idx_call_batches_due  ON call_batches (trigger_at) WHERE status = 'scheduled';
-- "Open" working set for the poller: running batches, plus cancelled batches that
-- still have unsettled targets. completed_at IS NULL is the exit condition — a
-- cancelled batch is stamped completed_at once drained (§5.2 phase 6) and leaves
-- this index forever (the sweep working set is bounded, not monotonic).
CREATE INDEX idx_call_batches_open ON call_batches (created_at)
    WHERE status IN ('running','cancelled') AND completed_at IS NULL;
```

### 3.3 `call_batch_targets`

```sql
CREATE TABLE call_batch_targets (
    id               BIGSERIAL PRIMARY KEY,
    batch_id         UUID NOT NULL REFERENCES call_batches(id) ON DELETE CASCADE,
    target_index     INTEGER NOT NULL,        -- position in the submitted array
    -- SET NULL (not CASCADE): a deleted elder must not silently shrink the
    -- batch; the poller marks the orphan target skipped/elder_deleted instead.
    elder_id         UUID REFERENCES elders(id) ON DELETE SET NULL,
    dynamic_vars     JSONB NOT NULL DEFAULT '{}',
    profile_override UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    skip_reason      TEXT,                    -- elder_deleted | invalid_timezone | key_conflict | daily_cap
    call_id          UUID REFERENCES calls(id) ON DELETE SET NULL,  -- root attempt
    final_status     TEXT,                    -- terminal CallStatus of the LAST attempt
    materialized_at  TIMESTAMPTZ,
    finalized_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_call_batch_targets_idx UNIQUE (batch_id, target_index),
    CONSTRAINT ck_call_batch_targets_status CHECK
        (status IN ('pending','materialized','done','skipped','cancelled'))
);
CREATE INDEX idx_call_batch_targets_pending ON call_batch_targets (batch_id, target_index)
    WHERE status = 'pending';
CREATE INDEX idx_call_batch_targets_open    ON call_batch_targets (batch_id)
    WHERE status IN ('pending','materialized');
CREATE INDEX idx_call_batch_targets_call    ON call_batch_targets (call_id)
    WHERE call_id IS NOT NULL;
```

Target lifecycle: `pending → materialized → done` (happy path); `pending → skipped` (elder deleted / invalid tz / key conflict / daily cap); `pending|materialized → cancelled` (batch cancel). `final_status` is a denormalized copy of the chain-terminal `CallStatus` (§6.2) so `GET /v1/batches/{id}` never walks retry chains at read time. Duplicate `elder_id`s within one batch are rejected at create with 422 (§4.2) — there is no path to N simultaneous campaigns against one elder from a single payload; cross-batch repetition is bounded by the daily cap (§5.3).

### 3.4 New `calls` index — counting in-flight calls

```sql
-- Concurrency gate (§5.4): counting dial-slot consumers must not scan the
-- monotonically-growing calls table, and the count is RECENCY-BOUNDED (a row
-- stuck IN_PROGRESS by a lost room_finished webhook must not consume a slot
-- forever), so the index key is updated_at under the static status predicate.
-- RINGING is included defensively (enum value, no writer yet).
CREATE INDEX idx_calls_in_flight ON calls (updated_at)
    WHERE status IN ('dialing','ringing','in_progress');
```

No `calls` column changes. Materialized roots use existing columns: `status='queued'`, `scheduled_at` (clamped dial time), `idempotency_key`, `dynamic_vars`, `profile_override`, `attempt=1`, `parent_call_id IS NULL`, fresh `livekit_room = f"usan-outbound-{uuid4()}"`.

## 4. API surface

All endpoints: `Depends(require_operator_token)` (static bearer, existing operator plane). Rate limiting is the existing **custom pre-auth `OperatorRateLimitMiddleware`** (`limits` package — there is no slowapi in this repo); its hardcoded allowlist `_is_operator_route` (`ratelimit.py`) **must gain `path.startswith("/v1/schedules")` and `path.startswith("/v1/batches")`** or the new endpoints ship completely unthrottled (unbounded brute force against the static key + unbounded batch-creation floods). This change and its route-matching test are in scope and listed in §9.

Every **mutating** endpoint (create/patch/delete/cancel) writes an audit record — client IP via the existing `client_ip()` helper, action, schedule/batch id, target count — following the existing PHI-access audit pattern in `routers/calls.py`. The plane remains identity-less (one shared key); per-user identity is a Phase A2 concern (§11).

Schema caps as module constants with rationale comments: `MAX_BATCH_TARGETS = 500`, `MAX_BATCH_NAME_LENGTH = 200`; `dynamic_vars` reuses `MAX_DYNAMIC_VARS_BYTES = 8192` from `schemas/call.py`. `days_of_week` travels as a list of lowercase day strings (`["mon","tue",...]`) and is stored as the bitmask. Times travel as `"HH:MM"` strings. Naive `trigger_at` is treated as UTC (precedent: `ScheduleCallbackRequest.requested_at`).

**Reserved key namespace:** `CreateCallRequest.idempotency_key` gains a validator rejecting values starting with `sched:` or `batch:` with 422 — these prefixes belong to the materializer (§2.2, §5.3).

**`profile_override` validation (schedules, batches, and per-target):** the referenced profile must exist, be ACTIVE, and have a published version, else 422 — `resolve_agent_config` silently falls through otherwise and the operator would believe an override is live when it is not. The override **is** consumed by the live agent today (runtime agent-config + SMS template resolution), so this is a behavioral feature, validated as one.

### 4.1 Schedules

| Method & path | Codes | Notes |
|---|---|---|
| `POST /v1/schedules` | **201**; 404 elder missing; 409 elder already has a schedule; 422 validation | Body: `elder_id`, `window_start_local`, `window_end_local`, `days_of_week` (default all 7), `enabled` (default true), `dynamic_vars` (default `{}`), `profile_override` (optional). 422 when the window does not intersect quiet hours `[09:00, 21:00)` (§6.3 — wall-clock check, tz-invariant), the elder's timezone fails zoneinfo resolution, days list is empty, vars exceed 8 KB, or `profile_override` fails validation. Computes and stores `next_run_at`. Audited. |
| `GET /v1/schedules?elder_id=&last_result=&limit=&offset=` | 200 | Bounded list (`MAX_SCHEDULES_LIMIT = 500`, clamped house-style), newest-first with `id` tiebreaker. `last_result=` filters on the §3.1 enum — `?last_result=skipped_window` is the "who missed today's call" view. |
| `GET /v1/schedules/{id}` | 200; 404 | |
| `PATCH /v1/schedules/{id}` | 200; 404; 422 | Partial update of `enabled`/window/days/`dynamic_vars`/`profile_override`; recomputes `next_run_at`; same 422 rules. Audited. |
| `DELETE /v1/schedules/{id}` | **204**; 404 | A call already materialized today is unaffected (it is an ordinary `Call`). Audited. Deleting (or disabling) a schedule is also the operator's PHI-removal path for its `dynamic_vars` (§8). |

`ScheduleResponse` (with `from_model`): all columns plus `days_of_week` rendered back as the string list, `next_run_at`, `last_materialized_date`, `last_result`, `last_result_at`.

### 4.2 Batches

| Method & path | Codes | Notes |
|---|---|---|
| `POST /v1/batches` | **201**; 200 idempotent replay; 409 key reuse w/ different payload; 422 | Body: `name`, `idempotency_key` (optional), `trigger_at` (optional), `window` (optional `{start_local, end_local, days_of_week}`), `max_concurrency` (optional ≥1), `profile_override` (optional), `targets: [{elder_id, dynamic_vars?, profile_override?}]` (1–500). Validation is all-or-nothing: every `elder_id` must exist, be unique within the batch, and every per-target `dynamic_vars` ≤8 KB, else **422** with `detail=[{target_index, error}]`. If `window` is set it must intersect quiet hours (422). **Replay:** the server stores `payload_digest` = sha256 over the canonical payload (name, trigger_at, window, days, max_concurrency, profile_override, and the ordered targets including vars/overrides — sorted-key JSON). Same `idempotency_key` + same digest → **200** with the existing batch; same key + different digest → **409**. This is strictly stronger than the cited `_idempotent_replay` precedent, which compares full payload content — "name and target count" alone would silently swallow a different target list. Batch and all targets are inserted in one transaction. Audited. |
| `GET /v1/batches?status=&limit=&offset=` | 200 | Summary rows: batch fields + counts `{pending, materialized, done, skipped, cancelled}`. |
| `GET /v1/batches/{id}` | 200; 404 | Detail: batch fields, counts, `final_status` histogram, and `targets[]`: `{target_index, elder_id, status, skip_reason, call_id, final_status, materialized_at, finalized_at}`. Per-target ordering by `target_index`. |
| `POST /v1/batches/{id}/cancel` | 200; 404; 409 if `completed` | Idempotent: cancelling a `cancelled` batch returns 200 unchanged. Semantics in §5.6. Audited. |

### 4.3 Call provenance

`GET /v1/calls/{id}` (`CallResponse`) gains a derived, read-only `origin` field parsed from the call's own `idempotency_key`: `{"source": "schedule"|"batch", "id": <uuid>, "ordinal": <local_date|target_index>}` for materialized roots, `null` otherwise. Retry children carry no key by design — they link to their root via `parent_call_id` (the documented chain walk). This closes the "no provenance" gap without new columns.

## 5. Scheduler / poller design

### 5.1 Process model

`schedule_orchestrator.py` exposes `poll_once(factory, settings, *, now=None)` and `run_poller(settings, stop)` with byte-for-byte the retry orchestrator's loop discipline: infinite loop, per-cycle exceptions logged-never-fatal, interval sleep = `asyncio.wait_for(stop.wait(), timeout=...)`. Wired in `main.py` lifespan as the third poller, gated on `scheduler_poller_enabled`, sharing the existing `stop` event and `finally` cancel/await, ahead of `background.drain()`.

Settings (all with UPPERCASE aliases, bounds, grouped comment block; mirrored into `infra/.env.example` — commented defaults, house style — `infra/.env.prod.example` — live values, house style — and `infra/docker-compose.yml` `services.api.environment` with `${VAR:-default}`; the dev compose file sets `SCHEDULER_POLLER_ENABLED=true` and `CONCURRENCY_GATE_ENABLED=true`):

| Field | Alias | Default | Bounds |
|---|---|---|---|
| `scheduler_poller_enabled: bool` | `SCHEDULER_POLLER_ENABLED` | **`False`** | feature-flag pattern |
| `scheduler_poll_interval_s: int` | `SCHEDULER_POLL_INTERVAL_S` | `60` | ge=15, le=600 |
| `scheduler_batch_size: int` | `SCHEDULER_BATCH_SIZE` | `50` | ge=1, le=500 |
| `concurrency_gate_enabled: bool` | `CONCURRENCY_GATE_ENABLED` | **`False`** | gate is independently disarmable (§5.4) |
| `max_concurrent_calls: int` | `MAX_CONCURRENT_CALLS` | `8` | ge=1, le=50 |
| `reserved_concurrency: int` | `RESERVED_CONCURRENCY` | `2` | ge=0, le=20 |
| `max_autonomous_calls_per_elder_per_day: int` | `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY` | `2` | ge=1, le=10 |
| `autonomous_dialing_paused: bool` | `AUTONOMOUS_DIALING_PAUSED` | **`False`** | emergency stop (§5.4, §10) |

A `model_validator` enforces `reserved_concurrency < max_concurrent_calls`. `MAX_CONCURRENT_CALLS=8` is sized for the e2-standard-4 single-VM reality (~5 simultaneous calls empirically saturated 2 vCPU; 8 on 4 vCPU is the conservative start — **measure before raising**, per the v0.1.0 overwhelm lesson). The daily-cap default of 2 allows the daily wellness schedule plus one batch campaign per elder per day.

With **both** flags at their `False` defaults the deploy is genuinely inert: no materialization happens *and* the retry poller's claim behavior is bit-identical to today (the gate is a no-op when disabled; only the gauges are exported — §5.4).

### 5.2 Poll cycle

One cycle = six phases, **each transaction kept short and committed before the next** (retry-orchestrator discipline), all claims `FOR UPDATE SKIP LOCKED`. Phases 3 and 4 process **one row per transaction** (claim 1, materialize, commit, repeat up to the phase budget): this holds exactly one per-phone advisory lock at a time — a time-sensitive `POST /v1/dnc` opt-out never queues behind a 50-lock batch transaction, and crash granularity is a single row.

1. **Finalize** settled batch targets (§6.2): for open batches' (`idx_call_batches_open`) `materialized` targets, resolve the attempt chain; settled → `done` + `final_status` + `finalized_at`.
2. **Trigger batches**: `scheduled AND (trigger_at IS NULL OR trigger_at <= now)` → `running` + `started_at`.
3. **Materialize due schedules**: claim `enabled AND next_run_at <= now ORDER BY next_run_at LIMIT scheduler_batch_size` (uses `idx_call_schedules_due`). Per row, in Python with zoneinfo (never SQL tz math — an invalid zone string must fail one row, not poison the cycle). Branches, exhaustive — every branch writes `last_result`/`last_result_at`:
   - **invalid timezone** (zoneinfo raises) → fail **closed**: no call, `last_result='skipped_invalid_timezone'`, ERROR log, `next_run_at = now + 1h` (retries hourly, observable, never hot-loops);
   - **`now` before effective-window start** (stale `next_run_at` after a westward elder-timezone edit or a window PATCH) → recompute `next_run_at` from the *current* timezone, no call, no `last_materialized_date` write, `last_result='rescheduled'` — defined behavior, no tight re-fire loop, no silently skipped day;
   - **`now` inside effective window** (= schedule window ∩ quiet hours; the intersection is wall-clock and tz-invariant, validated non-empty at create) → daily-cap and DNC checks then Call creation per §5.3; on success set `last_materialized_date = local_date`, `last_result='created'` (or `'replayed'`), advance `next_run_at` to the next masked day's effective-window start;
   - **`now` past window end** (poller was down) → **skip observably**: `last_result='skipped_window'`, metric, WARN log, advance `next_run_at` — never dial outside the operator's window.
4. **Materialize batch targets (throttled)**: compute `slots = max(0, max_concurrent_calls − reserved_concurrency − in_flight − queued_due)` where `in_flight` is the recency-bounded count of §5.4 and `queued_due = COUNT(*) WHERE status='queued' AND scheduled_at <= now` (served by `idx_calls_due_retries`, whose predicate matches exactly). Claim up to `min(scheduler_batch_size, slots)` `pending` targets of `running` batches, ordered `(batch_id, target_index)`; batches at their `max_concurrency` (count of unsettled targets' in-flight chains via `idx_call_batch_targets_call`) are passed over. **`max_concurrency` is a materialization throttle, not a dial-time cap**: it bounds how fast first attempts enter the queue; retry children of already-materialized chains dial whenever due, bounded only by the global gate — documented, not oversold. Per target: elder gone → `skipped/elder_deleted`; tz invalid → `skipped/invalid_timezone` (fail closed); daily cap hit → `skipped/daily_cap`; otherwise dial time = `quiet_hours.next_allowed(now, tz)` pushed into the batch window/day-mask if set, then create the Call (§5.3) and flip to `materialized`. **Priority note:** phase 3 (schedules) is deliberately unthrottled and runs first — the daily wellness call outranks campaign traffic; a 09:00-local schedule cohort will starve batch slots until its due-queue drains, by design.
5. **Sweep cancelled batches** (backstop only — the primary guard is the cancellation-aware `schedule_retry`, §5.6): for open `cancelled` batches (`idx_call_batches_open`), guarded-cancel any chain whose latest attempt is `QUEUED`.
6. **Complete batches**: `running` batches with zero open targets → `completed` + `completed_at`; `cancelled` batches with zero open targets → stamp `completed_at` (status stays `cancelled`), which removes them from `idx_call_batches_open` permanently — phases 1 and 5 never revisit drained history.

### 5.3 Materializing one Call (shared by phases 3 and 4)

One target/schedule row per transaction (call insert + that row's bookkeeping commit **atomically** — no crash window between them):

1. **Daily cap**: count autonomous roots for this elder (`idempotency_key LIKE 'sched:%' OR 'batch:%'`, served by `idx_calls_elder`) whose `scheduled_at` falls on the elder-local date. At or above `max_autonomous_calls_per_elder_per_day` → schedule: `last_result='skipped_daily_cap'`, advance `next_run_at`; batch target: `skipped/daily_cap`. The concurrency cap bounds *rate*; this bounds *repetition* — without it the same elder could be dialed by a schedule plus N batches in one day (harassment + TCPA exposure for exactly the population this product serves).
2. `dnc_repo.lock_phone(db, elder.phone_e164)` — the same advisory lock the enqueue path takes, serializing against concurrent `add_dnc`/enqueues. (One lock held at a time — see §5.2 preamble.)
3. **DNC check**: blocked → for a **batch target**, create the Call as `DNC_BLOCKED` (terminal, consumes the idempotency key — identical to `enqueue_call`'s gate); the target flips to `materialized` and the finalizer settles it to `done/final_status=dnc_blocked` next cycle. For a **schedule**, additionally set `enabled=false` + `last_result='dnc_blocked'` + WARN — a daily schedule must not mint one `DNC_BLOCKED` row per day forever; the operator re-enables after DNC removal (observable via `?last_result=dnc_blocked`). Never silent either way.
4. Otherwise create the Call: `status=QUEUED`, `scheduled_at=<clamped dial time>`, `idempotency_key` (`sched:{schedule_id}:{local_date}` / `batch:{batch_id}:{target_index}`), `dynamic_vars`, `profile_override` (target override wins over batch default), fresh `livekit_room`.
5. `IntegrityError` on the unique `idempotency_key` (re-poll after a partial crash, or a second replica) → SAVEPOINT rollback (the `schedule_retry` `begin_nested` pattern), re-fetch the existing row, then **verify ownership**: the existing row's `elder_id` must equal the target's elder and `parent_call_id IS NULL`. Match → adopt it **with the full created-branch bookkeeping** (schedule: advance `next_run_at` + set `last_materialized_date` + `last_result='replayed'`; batch: link `call_id` + flip `materialized`), metric `result="replayed"` — omitting the schedule bookkeeping here would re-claim and "replay" the same row every cycle forever. Mismatch (a squatted or foreign key — possible only for rows created before the §4 prefix rejection shipped) → ERROR log, batch target `skipped/key_conflict`, schedule `last_result='key_conflict'` + advance `next_run_at`; **never silently link a foreign call**. Either way, **a key can never dial twice**.

Dialing itself is **not** done here. The existing retry poller claims the row when `scheduled_at` comes due and runs `dispatch_and_dial` — inheriting the dial-time DNC re-check under the advisory lock, the **new dial-time quiet-hours re-check** (§2.3), built-in var re-resolution, dial classification, retry scheduling, stuck-DIALING reclaim (`scheduled_at IS NOT NULL` rows only — exactly ours), and the crash guard marking FAILED + retry.

### 5.4 Global concurrency gate (the hard cap)

`retry_orchestrator.poll_once` gains the single enforcement point. Inside the **same transaction** as `claim_due_retries` (count + claim share one snapshot — separate transactions would add avoidable intra-process drift from webhooks/ad-hoc dials):

1. `in_flight = COUNT(*) WHERE status IN ('dialing','ringing','in_progress') AND updated_at > now − (outbound_max_call_duration_s + 120s)` — served by `idx_calls_in_flight`. The **recency bound is load-bearing**: LiveKit enforces `max_call_duration` on every outbound dial, so any in-flight row older than that ceiling is wedged (lost `room_finished` webhook, lost agent end-call — only DIALING has a reaper today). Without the bound, eight wedged rows would silently and permanently halt **all** autonomous dialing — schedules, batches, *and pre-existing retries*. With it, a wedged row stops consuming a slot after the ceiling; the truthful gauges + alert (below) surface it.
2. Export gauges `usan_in_flight_calls` and `usan_dial_slots_free` (`max − reserved − in_flight`, floor 0) **here, every retry-poller cycle** — not in the scheduler cycle, which may be disabled while the gate is live. The gauges are exported even when the gate is disabled (pre-enable observability).
3. If `autonomous_dialing_paused` → claim **nothing** this cycle, log WARNING. This is the emergency stop: it freezes every poller-claimed dial (batch roots, schedule roots, retry children) while preserving all state — distinct from per-batch cancel (irreversible) and from `RETRY_POLLER_ENABLED=false` (which also kills stuck-DIALING reclaim and crash-marking). Resume by flipping it back.
4. If `concurrency_gate_enabled` → claim `LIMIT min(retry_batch_size, max(0, max_concurrent_calls − reserved_concurrency − in_flight))`, skipping the claim entirely at 0. Disabled → today's behavior, bit-identical.

Because **every** autonomous dial — batch root, schedule root, retry child — flows through this one claim, the cap is global by construction. Ad-hoc `POST /v1/calls` and inbound calls are *not* gated (no behavior change); `RESERVED_CONCURRENCY` keeps headroom for them (the RetellAI `reserved_concurrency` analog, inverted: we reserve *for* ad-hoc, away from the pollers). Note the deliberate double-conservatism: an active inbound/ad-hoc call both occupies a counted in-flight slot *and* the static reserve stays subtracted — correct, because the cap protects the VM and inbound load must shrink autonomous dialing, not share its budget. The batch-materialization throttle (§5.2 phase 4) is soft pacing that keeps the due-queue shallow so batch progress reporting reflects reality; the claim gate is the invariant.

The count-then-claim read is racy across replicas (bounded overshoot of one claim batch); with the documented single-replica deployment this is a non-issue, and it is stated in the module docstring next to the trunk-provisioning caveat.

### 5.5 Crash & restart semantics

- Claims are lock-scoped: a crash mid-transaction releases the lock; the row is re-claimed next cycle. Nothing is marked "in progress" outside a transaction.
- Call insert + bookkeeping are one commit per row (§5.3); the only cross-process duplicate window is closed by the unique idempotency key → verified replay path.
- A poller outage skips schedule occurrences whose windows fully elapsed (observable `skipped_window` + per-elder `last_result`, never a 23:00 call) and simply delays batch targets (no window expiry for `trigger_at`-only batches).
- Materialized rows stranded in `DIALING` by an API crash are reclaimed by the existing `reclaim_stuck_dialing` (they have `scheduled_at` set — the reaper's exact predicate).
- Shutdown: the scheduler commits claims before any dispatch happens (dispatch belongs to the retry poller), so it can be cancelled at any point; `background.drain(ringing_timeout + 15s)` and the compose `stop_grace_period` contract are unchanged.

### 5.6 Cancellation semantics

`POST /v1/batches/{id}/cancel`, in one transaction: batch → `cancelled` + `cancelled_at`; all `pending` targets → `cancelled`; for `materialized` targets, guarded `UPDATE calls SET status='cancelled' WHERE id = ANY(...) AND status='queued'` on each chain's latest attempt (first writer of the `CANCELLED` enum value). In-flight calls (`dialing`/`in_progress`) are **not** torn down — they finish naturally.

**Post-cancel retry children — primary guard at the source, not the sweep.** A sweep alone loses the race: FAILED retries are born at +1m, BUSY at +5m, and the retry poller claims every 30 s, while the sweep runs only each scheduler cycle (60 s default, up to 600 s) — a child born after cancel would frequently dial before any sweep reached it. Therefore `schedule_retry` becomes **cancellation-aware**: it walks `parent_call_id` to the chain root (≤3 hops), and if the root is batch-owned (join `call_batch_targets.call_id = root.id` via `idx_call_batch_targets_call`) with batch status `cancelled`, it returns `None` — no child is ever created, in the same commit as the parent's terminal transition, preserving the §6.2 one-commit invariant. The §5.2 phase-5 sweep remains as a belt-and-braces backstop for the narrow cancel-vs-terminal-transition commit race, bounded to open cancelled batches only.

`CANCELLED` is terminal and `schedule_retry`'s policy has no entry for it, so chains die there. The finalizer stamps swept/guard-cancelled chains `done/final_status=cancelled`; a chain whose last attempt finished naturally after cancel settles with its truthful outcome (e.g. `done/final_status=no_answer` with no further retries). `cancelled` targets stay `cancelled`. Schedules have no cancellation concept — deleting a schedule does not touch an already-materialized call (it is an ordinary `Call`), and the batch-cancellation check in `schedule_retry` does not apply to `sched:`-rooted chains.

## 6. Interaction with retry ladder, quiet hours, DNC

### 6.1 Retry ladder — inherited, with two deliberate changes

Materialized calls are roots (`attempt=1`, `parent_call_id NULL`). On NO_ANSWER/BUSY/FAILED/VOICEMAIL_LEFT, the existing `mark_* + schedule_retry` single-commit path creates at most one child per parent (partial unique index), clamped by quiet hours, per the unchanged ladder in `retry_policy.py` (NO_ANSWER +30m/+2h max 3 attempts; VOICEMAIL_LEFT +3h; BUSY +5m; FAILED +1m). Two changes to `schedule_retry` ship with this phase: it **copies `profile_override` to the child** (today it copies only `dynamic_vars`, silently reverting attempts 2..n to the default profile — a live behavioral bug the moment anything writes the column, §2.3), and it is **batch-cancellation-aware** (§5.6). **Batch calling windows constrain first attempts only**; retries follow the global ladder + quiet hours (ladder delays are ≤3 h, so drift past a narrow batch window is accepted and documented). This whole section is the differentiator over Retell, which retries nothing.

### 6.2 Terminal-status definitions (precise)

- **Chain-settled** ⇔ the chain's **latest attempt** (max `attempt` via the `parent_call_id` linked list, depth ≤3) satisfies: `status ∉ {queued, dialing, ringing, in_progress}` **AND** it has no child row. This is sound because every terminal transition and its `schedule_retry` happen in **one commit** — after that commit, either the child exists or it never will (including the fail-closed no-schedule paths: invalid timezone, elder deleted, ladder exhausted, **batch cancelled**). The one-child-max index makes the "no child" probe a single indexed lookup.
- **`final_status`** = the latest attempt's status: one of `completed, voicemail_left, no_answer, busy, failed, dnc_blocked, cancelled` (bounded 7-value set; `voicemail_left` is chain-terminal only when its retry was exhausted or not scheduled).
- **Target done** ⇔ chain-settled (finalizer stamps `done`, `final_status`, `finalized_at`).
- **Batch done** ⇔ every target ∈ `{done, skipped, cancelled}` → batch `completed` (or, for a cancelled batch, `completed_at` stamped — §5.2 phase 6).

### 6.3 Quiet hours (TCPA) — enforced four times

`quiet_hours.next_allowed` (`[09:00, 21:00)` elder-local, zoneinfo-only DST handling, `ValueError` on bad zone) applies at:
1. **API validation**: a schedule/batch window that does not intersect `[09:00, 21:00)` is rejected with **422** — a window that can never fire cannot be created (wall-clock check; tz-invariant, so it cannot rot).
2. **Materialization**: every dial time is clamped before being written to `scheduled_at`; invalid timezone fails closed to an observable skip (`skipped_invalid_timezone`), never a dial. A clamp that lands past the schedule's window end → `skipped_window`, never silent.
3. **Dial time (new, §2.3)**: `dispatch_and_dial` re-checks `next_allowed` against the *actual* dial moment — gate-induced waiting, poller restarts, and inbound-flood slot saturation can all slide a claim minutes-to-hours past its clamp, and a clamp is a promise about the past. Outside quiet hours → re-queue with a fresh clamp, metric, WARNING; never dial. (Window-only drift *within* statutory hours is accepted, same as retry drift — the statutory boundary is the hard line.)
4. **Retry scheduling**: unchanged existing clamp in `schedule_retry`.

The current enqueue path's "dials immediately, no TCPA gate" gap therefore does not extend to any scheduled/batch call — and #3 closes it for the autonomous dial path generally.

### 6.4 DNC — enforced twice (+ schedule auto-disable)

At materialization (advisory lock + check → terminal `DNC_BLOCKED` row; schedules additionally auto-disable, §5.3) **and** at dial time (`dispatch_and_dial`'s re-check under the same lock — already in the dial path). A number added to the DNC list between materialization and a future-dated `scheduled_at` is still caught.

## 7. Observability

New counters in `observability/custom_metrics.py` (constructed as `usan_X` → `_total`; labels bounded, PHI-free — never ids, phones, or free text):

| Metric | Labels | Incremented |
|---|---|---|
| `usan_materialized_calls_total` | `source ∈ {schedule, batch}`, `result ∈ {created, replayed, dnc_blocked, skipped_window, skipped_invalid_timezone, skipped_daily_cap, skipped_elder_deleted, rescheduled, key_conflict}` | per materialization decision, after commit. Some combinations are structurally impossible and never emitted: `skipped_elder_deleted`, with `source="schedule"` (CASCADE deletes the schedule with its elder); `skipped_window`/`rescheduled` with `source="batch"` (targets have no `next_run_at`). Documented in the module. |
| `usan_batch_events_total` | `event ∈ {created, started, completed, cancelled}` | at each batch transition, after commit |
| `usan_batch_targets_finalized_total` | `final_status` (the 7-value set, §6.2) | finalizer, after commit |
| `usan_dial_requeued_total` | `reason ∈ {quiet_hours}` | dial-time quiet-hours re-check (§2.3) |
| `usan_in_flight_calls` (Gauge) | — | set each **retry-poller** cycle from the §5.4 recency-bounded count |
| `usan_dial_slots_free` (Gauge) | — | set each **retry-poller** cycle (`max − reserved − in_flight`, floor 0) |

The gauges live in the retry poller — the component that computes and enforces the gate — so they are truthful whenever the gate could act, including with the scheduler disabled. **One alert rule ships as code this phase** (house precedent: urgent-flag + SMS-failure alerts are already provisioned as code): `usan_dial_slots_free == 0` sustained ≥10 min while `CONCURRENCY_GATE_ENABLED=true` — with the gate live, zero slots means autonomous dialing has stopped, which for a wellness-check product is a paging condition, not a dashboard curiosity. Grafana *panels* stay out of scope.

Per-elder schedule outcomes are queryable via `last_result` (§3.1/§4.1); batch progress via `GET /v1/batches/{id}` counts (the operator's primary view). Log lines bind ids only: `logger.bind(schedule_id=…, batch_id=…, target_index=…, call_id=…)` — never `name` (§8); skips log at WARNING, fail-closed paths at ERROR. Prometheus increments happen **after** commit (Phase-3 discipline: a crash can't double-count).

## 8. Security

- **Authz**: every new endpoint sits on the existing operator plane (`require_operator_token`, static `OPERATOR_API_KEY` bearer ≥16 chars). No new auth surface; admin-session plane untouched (Admin-UI is Phase A2). The pollers run unauthenticated in-process, same trust level as the retry poller. All mutating endpoints write audit records (IP, action, ids, target count — §4); the plane remains identity-less, which is acknowledged residual risk until Phase A2 (§11).
- **Rate limiting**: `_is_operator_route` in `ratelimit.py` is **extended** with the `/v1/schedules` and `/v1/batches` prefixes (the middleware is allowlist-based; without this change the endpoints would be entirely unthrottled — key brute force + creation floods). Covered by a route-matching test (§9).
- **Volume-abuse limits (scoped to poller-claimed dials)**: `MAX_BATCH_TARGETS=500` per batch (422 above), duplicate elders per batch rejected (422), `dynamic_vars` ≤8 KB per target, bounded list reads (≤500), the hard `MAX_CONCURRENT_CALLS` ceiling on the **poller-claimed** dial rate, and `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY` on per-elder repetition of autonomous roots. A runaway operator script can fill tables but cannot accelerate poller-claimed dialing or harass an individual elder *through the schedule/batch plane*. `AUTONOMOUS_DIALING_PAUSED` is the state-preserving emergency stop for that plane (§5.4, §10).
- **Ad-hoc `POST /v1/calls` is NOT covered by these ceilings** (pre-existing path, unchanged this phase): it dials immediately and bypasses the quiet-hours dial-time re-check, the daily cap, the concurrency gate, and `AUTONOMOUS_DIALING_PAUSED` — its only controls are the operator key and rate limiting. Bringing it under the same umbrella is a tracked follow-up (§11).
- **TCPA**: quadruple quiet-hours enforcement including the dial-time re-check (§6.3), double DNC enforcement + schedule auto-disable (§6.4), fail-closed timezone handling at every clamp point, per-elder daily cap. Consent posture unchanged: only enrolled elders (rows in `elders`) are addressable; there is no free-form phone-number target.
- **PHI**: per-target/schedule `dynamic_vars` reuse the existing 8 KB JSONB channel and the existing egress path (LiveKit dispatch metadata — already inside the PHI boundary); **no new PHI egress**. **Retention is extended, not "unchanged"**: `retention.purge_expired` gains, in its existing single transaction, a scrub of `call_batch_targets.dynamic_vars` for settled targets (`status ∈ {done, skipped, cancelled}`) past the `PHI_RETENTION_DAYS` cutoff — without this, the `calls` copy is scrubbed while the source copy lives forever, defeating the control. `call_schedules.dynamic_vars` are deliberately **exempt** as live re-used config; the documented operator PHI-removal path for a schedule is `PATCH` (clear vars) or `DELETE` — stated in the API docs and router docstring. `call_batches.name` is PHI-free **by convention only** (it will be tempting to type an elder's name into it): the ids-only log-bind rule, a router-docstring warning, and a test asserting batch log lines never bind `name` keep it out of logs, and §8 acknowledges the list-response exposure as residual risk, not a guarantee. Metric labels are bounded enums only.
- **Idempotency-key namespace**: `sched:`/`batch:` prefixes are reserved (422 in `CreateCallRequest`) and the replay path verifies elder ownership before adopting a row (§5.3) — predictable keys cannot be squatted to suppress or substitute a wellness call.
- **Secrets/config**: no new secrets. New env keys validated at startup via `Settings` bounds + the cross-field reserved<max validator.

## 9. Testing strategy (TDD — tests written first per task)

**Migration contract** — `tests/test_batch_migration.py` (mirror of `test_phase3_migration.py`): after alembic head, assert all three tables, columns (incl. `last_result`, `payload_digest`), CHECK constraints, partial indexes (incl. `idx_calls_in_flight` on `updated_at` and `idx_call_batches_open`'s `completed_at IS NULL`), and FK delete behaviors exist; downgrade drops cleanly.

**Unit (pure, no DB)** — `schedule_windows.py`: day-mask round-trip (`["mon"]`↔bitmask), effective-window intersection with quiet hours (empty ⇒ error; tz-invariance), `next_run_at` computation across DST transitions (America/New_York spring-forward 02:30 window — zoneinfo-recompute landmine from `quiet_hours.py`), window-elapsed and before-window-start detection. Schema validators: 8 KB vars cap, window/day 422s, duplicate-elder 422, naive `trigger_at`→UTC, reserved-prefix rejection in `CreateCallRequest`, canonical payload digest stability (key order, target order).

**Repository/integration (pg container, conftest TRUNCATE updated)** —
- *Materialization idempotency*: run scheduler `poll_once` twice with frozen `now` → exactly one `Call` per schedule/target; second pass counts `replayed` **and** performs full bookkeeping (schedule `next_run_at` advanced — regression test for the infinite re-claim loop).
- *Crash-window replay + ownership*: pre-insert a call with a target's idempotency key and matching elder → replay links `call_id`; pre-insert with a **different** elder → `skipped/key_conflict`, ERROR, no link.
- *Race*: two concurrent `poll_once` against the same due rows → SKIP LOCKED yields disjoint claims, key uniqueness holds.
- *Concurrency gate*: seed N in-flight rows → retry-poller claim limit shrinks to `max − reserved − N`; zero slots ⇒ zero claims; **stale in-flight rows (old `updated_at`) are excluded from the count**; `autonomous_dialing_paused` ⇒ zero claims; gate disabled ⇒ claim behavior bit-identical to today (flag matrix: gate × scheduler on/off); batch `max_concurrency` passes over a saturated batch; gauges exported from the retry cycle.
- *Daily cap*: schedule + two batches against one elder same local date → third root skipped `daily_cap`.
- *DNC at materialization*: blocked number → batch: `DNC_BLOCKED` row, target `materialized`, finalizer → `done/dnc_blocked`; schedule: auto-disabled + `last_result='dnc_blocked'`.
- *Quiet hours/window*: poller down past window end → `skipped_window`, `next_run_at` advanced, no call; before-window-start (tz edit) → `rescheduled`, no call; invalid tz → fail-closed skip, hourly retry; **dial-time re-check**: claimed row outside quiet hours → re-queued with fresh clamp, `usan_dial_requeued_total` incremented, no dispatch.
- *Dispatch guard*: call with `elder_id NULL` → marked FAILED `elder_missing` (regression test for the DIALING↔QUEUED ping-pong), chain settles.
- *Finalizer matrix*: completed chain; no_answer with pending child (unsettled); ladder-exhausted no_answer (settled); fail-closed-no-child (settled); voicemail chain.
- *Cancellation*: pending→cancelled; queued root guarded-cancel; in-flight untouched; **post-cancel FAILED(+1m) child is never created (`schedule_retry` cancellation-aware) even with `SCHEDULER_POLL_INTERVAL_S` at max** — the race the sweep alone would lose; sweep backstop; drained cancelled batch gets `completed_at` and leaves `idx_call_batches_open`; cancel idempotent; completed→409.
- *Retry inheritance*: `profile_override` survives to attempt 2..n.
- *Retention*: settled batch-target `dynamic_vars` scrubbed past cutoff in the same `purge_expired` transaction; schedules untouched.
- *Batch completion*: all-terminal targets flip the batch exactly once.

**API integration** — every endpoint × status code in §4 tables, including digest-replay 200 vs 409 divergence, per-target 422 detail shape, `profile_override` validation 422s, `?last_result=` filter, derived `origin` field, audit records written for each mutation, and a log-capture test asserting batch log lines never bind `name`.

**Middleware/lifespan** — rate-limit route-matching test: `/v1/schedules` and `/v1/batches` match `_is_operator_route` (precedent: existing ratelimit tests); lifespan wiring test for the third poller (precedent: `tests/test_lifespan_poller.py`); metric-increment assertions (precedent: `test_observability.py`).

**Infra contract** — `tests/test_infra_scheduler_env.py` (clone of `test_infra_messaging_env.py`): all **eight** new keys present in `infra/docker-compose.yml` api environment, `infra/.env.example` (commented-default style), `infra/.env.prod.example` (live-value style); additionally pins dev-compose `SCHEDULER_POLLER_ENABLED=true` + `CONCURRENCY_GATE_ENABLED=true` and the provisioned alert rule file.

Gates: coverage ≥80 % on new modules; `ruff check`, `ruff format`, and **mypy** (CI runs it even though CLAUDE.md doesn't say so) green before push.

## 10. Rollout & ops

1. **Ship inert — genuinely.** Migration `0012` + code deploy with `SCHEDULER_POLLER_ENABLED=false` **and** `CONCURRENCY_GATE_ENABLED=false` (both settings defaults). Tables and endpoints exist; nothing dials autonomously; the retry poller's claim behavior is bit-identical to today (the gate is flag-isolated precisely so "inert" is true — only the new gauges appear). Dev compose enables both flags for local testing.
2. **Deploy mechanics** (project-specific, load-bearing): merging to `main` changes nothing on the VM — cut a `v*` tag for app+compose, and update `/opt/usan/infra/.env` on the VM (IAP SSH or reboot-refetch) **before** the tag deploy, since the deploy never re-fetches the secret/env file. New keys without the .env refresh ⇒ compose interpolates defaults (safe here: everything defaults off).
3. **Validation sequence**: (a) flip `CONCURRENCY_GATE_ENABLED=true` first and observe the gauges + existing retry traffic for a day — the gate is exercised by real load before any batch exists; (b) flip `SCHEDULER_POLLER_ENABLED=true`, create one test schedule against a test elder, observe `usan_materialized_calls_total{source="schedule",result="created"}` and the live call end-to-end (including a forced quiet-hours re-queue); (c) run a small (≤5-target) batch. Watch `usan_in_flight_calls` against the `MAX_CONCURRENT_CALLS=8` budget on the e2-standard-4 before any cap raise.
4. **Emergency stop & rollback runbook** (in order of increasing severity):
   - *Pause* (reversible, state-preserving): set `AUTONOMOUS_DIALING_PAUSED=true` + `compose up` — every poller-claimed dial stops; in-flight calls finish; resume by flipping back. **Poller-claimed dials only**: ad-hoc `POST /v1/calls` keeps dialing immediately, outside this stop and the §8 ceilings.
   - *Stop one campaign*: `POST /v1/batches/{id}/cancel` (irreversible for that batch; in-flight calls finish; post-cancel retry children are suppressed at `schedule_retry`).
   - *Stop materialization*: `SCHEDULER_POLLER_ENABLED=false` — note this does **not** stop already-materialized QUEUED rows, which ride the retry poller; pause or cancel for those. Disabling the scheduler also stops the **phase-5 cancelled-batch sweep**, so cancel a batch *before* disabling the scheduler (or re-enable it to drain) — otherwise a re-queued root of a cancelled batch can sit QUEUED with nothing left to sweep it.
   - *Image rollback*: roll the `v*` tag back **only after draining** — old code's retry poller will happily claim materialized rows with **no gate, no cancel endpoint, no batch bookkeeping**. Sequence: scheduler off → cancel open batches → pause → verify drain with `SELECT count(*) FROM calls WHERE status='queued' AND (idempotency_key LIKE 'sched:%' OR idempotency_key LIKE 'batch:%')` = 0 → roll back. Break-glass (old image already live): the same predicate as an `UPDATE … SET status='cancelled'`.
   - *Migration*: the entrypoint auto-runs `alembic upgrade head`, so a rolled-back image never auto-downgrades; `0012` is purely additive and inert under old code. Run `alembic downgrade` manually only after the drain check.
5. **Single-replica**: documented in the orchestrator docstring (count-then-claim overshoot + the existing trunk-provisioning caveat); pin `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` before ever scaling out.
6. **Shutdown contract unchanged**: scheduler claims commit before dispatch; compose `stop_grace_period` already exceeds `ringing_timeout + 15s`.
7. This phase **closes the "daily-call scheduler is unowned" cutover blocker**: with one `CallSchedule` row per enrolled elder, the RetellAI daily wellness flow is fully in-repo.

## 11. Open questions

1. **Stuck-IN_PROGRESS reaper** — the §5.4 recency bound removes the gate-starvation exposure, but a wedged IN_PROGRESS row still never reaches a terminal state on its own (target unsettled until manual intervention). A true reaper must reconcile carefully with late `room_finished` webhooks and agent outcome reports; deferred with the recency bound + alert as the mitigation.
2. **Day-2 batch affordances** — `PATCH /v1/batches/{id}` (postpone `trigger_at`, tune `max_concurrency`, rename), per-target cancel, "re-run failed targets" of a completed batch, and a schedule "run-now" test-fire are all deliberately deferred to keep this phase's API surface fixed; the Phase A2 admin-UI work is the natural vehicle.
3. **Operator-plane identity** — audit records (IP + action) ship now, but the shared static key remains identity-less; moving these endpoints to the admin-session plane (per-user, role=admin) is a Phase A2 decision.
4. **Schedule cadence** — one occurrence per elder-local date is deliberate; if "twice daily" ever becomes a requirement, it needs a schedule-occurrence key change (`sched:{id}:{date}:{slot}`), not a redesign.
5. **Batch window vs retry drift** — retries may land outside a narrow batch window within statutory hours (§6.1). Acceptable now; revisit if a customer needs strict-window campaigns (would require a window-aware `schedule_retry` hook).
6. **Pacing** — calls-per-minute shaping (beyond the concurrency cap) deferred until measured VM headroom justifies a bigger cap.
7. **Mid-call teardown on cancel** — deferred; needs LiveKit room deletion + agent-side handling to be graceful.
8. **`RINGING` writer** — still unwritten; the in-flight index and gate already count it defensively if a future dial path starts using it.
9. **Bring ad-hoc `POST /v1/calls` under the TCPA/daily-cap/pause umbrella** — today it dials immediately, outside the quiet-hours dial-time re-check, the daily cap, the concurrency gate, and `AUTONOMOUS_DIALING_PAUSED` (§8). Queueing it through the same claim path instead of the immediate dial would close the gap; tracked follow-up.

---

## Appendix A — adversarial review findings disposition

| Finding (lens — severity) | Disposition |
|---|---|
| Gate starvation via unreapable in-flight rows; no kill switch; gauges dead when scheduler off (ops C1, sec/corr MEDIUM) | **Applied** §3.4/§5.4: recency-bounded count, `CONCURRENCY_GATE_ENABLED` + `AUTONOMOUS_DIALING_PAUSED` flags, gauges moved to retry poller, alert rule in scope |
| No dial-time quiet-hours re-check (sec HIGH) | **Applied** §2.3/§6.3: re-check in `dispatch_and_dial`, re-queue + metric, never dial on stale clamp |
| slowapi claim false; endpoints unthrottled (all lenses HIGH) | **Applied** §4/§8: `_is_operator_route` extended, wording fixed, route test added |
| PHI retention gap on new `dynamic_vars` (sec/ops HIGH) | **Applied** §8: settled-target scrub in `purge_expired` this phase; schedules exempt, operator path documented |
| `profile_override` "recorded-not-applied" wrong; retry children lose it (ops H2, sec/corr MEDIUM) | **Applied** §2.3/§4/§6.1: corrected (live today), create-time validation, `schedule_retry` copies it; ex-open-question removed |
| Cancelled-batch sweep loses race to retry poller (corr HIGH, ops M2) | **Applied** §5.6: `schedule_retry` cancellation-aware (primary), sweep demoted to backstop |
| elder-deleted DIALING↔QUEUED ping-pong (corr HIGH) | **Applied** §2.3: guard marks FAILED `elder_missing` |
| Per-elder skip observability (ops H3) | **Applied** §3.1/§4.1: `last_result`/`last_result_at` + list filter |
| No rollback runbook (ops H4) | **Applied** §10.4: pause/cancel/drain/break-glass + migration note |
| Replay branch omits bookkeeping → schedule infinite loop (corr MEDIUM) | **Applied** §5.3 step 5: full created-branch bookkeeping on replay |
| Key squatting / foreign-row adoption (sec/corr MEDIUM/LOW) | **Applied** §2.2/§4/§5.3: reserved prefixes 422 + ownership verification, `key_conflict` outcome |
| No "now < window start" branch (corr MEDIUM, sec LOW) | **Applied** §5.2 phase 3: `rescheduled` branch; bogus "window stops intersecting quiet hours" branch removed (intersection is tz-invariant) |
| Batch replay weaker than precedent (all lenses MEDIUM) | **Applied** §3.2/§4.2: canonical `payload_digest`, 409 on divergence |
| No per-elder dedup / daily ceiling (sec MEDIUM, corr LOW) | **Applied** §4.2/§5.3: duplicate-elder 422 + `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY` |
| No emergency stop (sec MEDIUM) | **Applied** §5.4/§10.4: `AUTONOMOUS_DIALING_PAUSED` |
| `max_concurrency` oversold (corr MEDIUM) | **Applied** §3.2/§5.2: documented as materialization throttle |
| Cancelled batches never leave working set (corr MEDIUM, sec/ops LOW/M5) | **Applied** §3.2/§5.2 phase 6: `completed_at` stamp + `idx_call_batches_open` exit condition |
| Identity-less mass-dial plane (sec MEDIUM) | **Partially applied** §4/§8: audit records now; plane migration deferred (open Q3) |
| "Ship inert" isn't inert (ops M1) | **Applied** §5.1/§10.1: gate flag-isolated, both defaults off |
| `idx_calls_status_scheduled` "nonexistent" (ops M3) | **Finding incorrect** — both indexes exist (verified 0001:88-90, 0003:28-30); spec now cites `idx_calls_due_retries` (exact predicate match) |
| Day-2 affordances missing (ops M6) | **Partially applied**: provenance via `origin` (§4.3), DNC auto-disable + `last_result` (§5.3); PATCH-batch/per-target-cancel/re-run/run-now deferred (open Q2) |
| Test gaps (ops M7) | **Applied** §9: lifespan, ratelimit, metrics, flag matrix, cancel race, override inheritance, dev-compose pin |
| Advisory-lock accumulation (sec/corr LOW) | **Applied** §5.2: one row per transaction, one lock at a time |
| `window_never_open` dead enum (ops L2) | **Applied** §3.3: dropped (create-time validation makes it unreachable) |
| Reserved-concurrency double-count (corr/ops LOW/L1) | **Applied** §5.4: documented as deliberate conservatism |
| Count+claim transaction (corr LOW) | **Applied** §5.4: same transaction |
| Phase-3 starves phase 4 (corr LOW) | **Applied** §5.2 phase 4: schedules-over-batches priority documented as deliberate |
| `name` PHI residual risk (sec LOW) | **Applied** §8: convention + log-bind test + residual-risk acknowledgment |
| Env-file style ambiguity (ops L3) | **Applied** §5.1: commented defaults vs live values stated |
| Impossible metric combos (ops L4) | **Applied** §7: impossible combinations documented per source |
