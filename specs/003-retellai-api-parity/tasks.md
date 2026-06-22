---
description: "Task list for RetellAI-Compatible Public API"
---

# Tasks: RetellAI-Compatible Public API

**Input**: Design documents from `specs/003-retellai-api-parity/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: REQUIRED. The constitution makes Test-First NON-NEGOTIABLE (Principle IV, ≥80% coverage),
so every story leads with tests that MUST be written first and confirmed FAILING before
implementation. The acceptance oracle is the captured CRM RetellAI-usage inventory (spec Dependencies).

> **⚠️ Contract-freeze gate (do this before freezing the contract tests T017 / T031 / T038 / T041):**
> Capture the CRM's actual RetellAI traffic and **pin** the "open contract items" listed in
> [contracts/endpoints.md](./contracts/endpoints.md#open-contract-items--pin-against-the-captured-crm-oracle)
> — `metadata.external_id`/`name` key placement, `delivery_id` transmission (header vs body),
> `override_agent_version` string form, `custom_sip_headers` rules, and the exact prefix/name of
> `list-retell-llms` / `publish-agent-version`. Those tasks' assertions MUST encode the pinned shapes,
> not guesses. T047 completes the gate (mark "CONTRACT FROZEN").

**Organization**: Tasks are grouped by user story (US1–US5) for independent implementation/testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story a task serves (US1–US5); Setup/Foundational/Polish carry no story label
- Every task names an exact file path. All paths are under `apps/api/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Scaffolding, config, and the one schema change.

- [X] T001 Create the compat subpackage skeleton: `apps/api/src/usan_api/compat/__init__.py`, `apps/api/src/usan_api/compat/schemas/__init__.py`, `apps/api/src/usan_api/compat/routers/__init__.py`
- [X] T002 [P] Add compat settings (`COMPAT_DOCS_ENABLED` bool, `COMPAT_WEBHOOK_ALLOWED_HOSTS` list, `COMPAT_DEFAULT_TIMEZONE` IANA str, compat rate-limit bucket) with Pydantic validation in `apps/api/src/usan_api/settings.py`
- [X] T003 [P] Create Alembic migration `apps/api/migrations/versions/0036_compat_api_keys.py` — global (non-RLS) `compat_api_keys` table per [data-model §1](./data-model.md#1-new-entity--compat_api_keys-global--non-rls) (`organization_id` FK, `key_prefix` indexed, `key_hash`, `status`, `label`, timestamps)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Auth plane, error envelope, id/serialization helpers, and the mounted sub-app — every
user story depends on these.

**⚠️ CRITICAL**: No user-story work begins until this phase is complete.

### Tests for Foundational (write FIRST, confirm FAILING) ⚠️

- [X] T004 [P] Auth tests (valid/invalid/revoked key → 401; valid key sets the org RLS context) in `apps/api/tests/test_compat_auth.py`
- [X] T005 [P] Mount-isolation tests (`/health` + a `/v1` route still hit native handlers after `app.mount`; startup collision assertion fires on a shadow path; compat errors are `{status,message}` while `/v1` stays `{detail}`) in `apps/api/tests/test_compat_mount_isolation.py`
- [X] T006 [P] RLS-isolation tests (an org-A key returns zero org-B calls/agents/batches, incl. post-commit reads) in `apps/api/tests/test_compat_rls_isolation.py`

### Implementation for Foundational

- [X] T007 [P] Implement `CompatError` + the four exception handlers (`CompatError`, `StarletteHTTPException`, `RequestValidationError`, catch-all `Exception`) emitting the RetellAI `{status,message}` envelope, in `apps/api/src/usan_api/compat/errors.py`
- [X] T008 [P] Implement the reversible id codec (`call_id` bare 32-hex; `agent_`/`llm_`/`batch_call_` prefixes; decode validates + 422 on malformed) in `apps/api/src/usan_api/compat/ids.py`
- [X] T009 [P] Implement serialization helpers (`to_ms(datetime)->int`, `duration_ms`) in `apps/api/src/usan_api/compat/serialization.py`
- [X] T010 [P] Implement Pydantic models for admin key issue/list/revoke (token returned once) in `apps/api/src/usan_api/schemas/compat_api_keys.py`
- [X] T011 Implement the `compat_api_keys` repository (`lookup_active_by_prefix`, `create` return-once, `list`, `revoke`, `touch_last_used`) in `apps/api/src/usan_api/repositories/compat_api_keys.py` (depends on T003)
- [X] T012 Implement the `get_compat_db` dependency (parse Bearer via `HTTPBearer(auto_error=False)`, `token[:8]` prefix lookup, `hmac.compare_digest(sha256_hex(token), key_hash)`, open session + `tenant_context.set_tenant_context` with the `after_begin` re-apply listener, best-effort `touch_last_used`, audit line org+key-id) in `apps/api/src/usan_api/compat/auth.py` (depends on T011)
- [X] T013 Implement the native admin key router (`POST/GET /v1/admin/compat-keys`, `DELETE /v1/admin/compat-keys/{id}`, super-admin guarded) in `apps/api/src/usan_api/routers/admin_compat_keys.py` (depends on T010, T011)
- [X] T014 Build the mounted compat sub-app (`FastAPI` `compat_app`: title/version, `COMPAT_DOCS_ENABLED` docs/OpenAPI toggle, app-level `Depends(get_compat_db)` baseline, register the exception handlers + the three routers) in `apps/api/src/usan_api/compat/app.py` (depends on T007, T012)
- [X] T015 Wire `compat_app` into the app factory — `app.mount("/", compat_app)` AFTER all native routers + `/health`, register `admin_compat_keys.router`, add the startup path-collision assertion over `app.routes` — in `apps/api/src/usan_api/main.py` (depends on T013, T014)
- [X] T016 [P] Extend the rate limiter with a compat-aware bucket for the CRM key (FR-054) in `apps/api/src/usan_api/ratelimit.py`

**Checkpoint**: Foundation ready — user stories can begin (in parallel if staffed).

---

## Phase 3: User Story 1 — Drop-in outbound calling (Priority: P1) 🎯 MVP

**Goal**: Place & track calls via the RetellAI-shaped call endpoints with a bare base-URL + key repoint.

**Independent Test**: With an issued key, `POST /v2/create-phone-call` for a never-seen `to_number`
returns 201 + a Call object; `GET /v2/get-call/{id}` and `POST /v3/list-calls` return it with RetellAI
field names + ms timestamps; a DNC/quiet-hours number returns an explicit 400.

### Tests for User Story 1 (write FIRST, confirm FAILING) ⚠️

- [X] T017 [P] [US1] Contract tests for `/v2/create-phone-call` (201), `/v2/get-call`, `/v3/list-calls` (filter+cursor+has_more/total), `/v2/stop-call` (204), `/v2/update-call` — paths, field names, status codes, `{status,message}` errors — in `apps/api/tests/test_compat_calls.py`
- [X] T018 [P] [US1] Integration tests: full create→get→list→stop→update lifecycle; number→Contact lazy upsert (name/timezone defaults); synthesized-idempotency no-double-dial on retry; DNC and quiet-hours each return an explicit 400 with a machine-readable reason in `apps/api/tests/test_compat_calls_integration.py`

### Implementation for User Story 1

- [X] T019 [P] [US1] Implement the `CallStatus` ⇄ Retell `call_status` + `disconnection_reason` mapping per [data-model §4](./data-model.md#4-call-status--disconnection-reason-mapping-compatstatus_mappy) in `apps/api/src/usan_api/compat/status_map.py`
- [X] T020 [P] [US1] Implement Pydantic schemas for create-phone-call / get-call / list-calls (v3 `filter_criteria` + cursor) / stop-call / update-call + the shared **Call object** in `apps/api/src/usan_api/compat/schemas/calls.py`
- [X] T021 [US1] Implement the Call serializer — assemble the full RetellAI Call object (ms timestamps, `transcript_object`, `recording_url`, `call_analysis`, `disconnection_reason`, `telephony_identifier`) from Call + transcript + recording + metrics rows — in `apps/api/src/usan_api/compat/call_serializer.py` (depends on T009, T019, T020)
- [X] T022 [US1] Implement the call-create service — `to_e164` + Contact lazy upsert (reuse `contacts_repo.get_contact_by_phone`/`create_contact`; default name=E.164, timezone=`COMPAT_DEFAULT_TIMEZONE`, preserve `metadata.external_id`); synthesized deterministic `idempotency_key` (namespaced outside `sched:`/`batch:`); create-time DNC (`dnc_repo.is_blocked`) + quiet-hours (`quiet_hours.next_allowed`) gating → raise `CompatError(400, "blocked_dnc"|"blocked_quiet_hours")` before dialing; else reuse `dnc_repo.lock_phone`→`calls_repo.create_call`→`livekit_dispatch.dispatch_agent`→`dialer.schedule_dial` — in `apps/api/src/usan_api/compat/call_create.py`
- [X] T023 [US1] Implement the `/v2` calls router + `/v3/list-calls` (create→201, get, stop→204, update, list with cursor pagination + `has_more`/`total`) in `apps/api/src/usan_api/compat/routers/calls.py` and register it on `compat_app` (depends on T021, T022)
- [X] T024 [US1] Add structured loguru audit logging (org + key id, never token/PHI) on every calls-router operation, per Constitution VI / FR-055, in `apps/api/src/usan_api/compat/routers/calls.py`

**Checkpoint**: US1 fully functional and independently testable → **MVP**.

---

## Phase 4: User Story 2 — Call-event webhooks (Priority: P1)

**Goal**: Emit `call_started`/`call_ended`/`call_analyzed` in the RetellAI `{event, call}` envelope with
an `x-retell-signature` the CRM's `retell-sdk verify()` accepts, full-fidelity to allow-listed hosts.

**Independent Test**: Configure an agent `webhook_url` on an allow-listed host, place a call, and assert
the receiver gets the three events; `Retell.verify(raw_body, api_key, signature)` returns True; a
non-allow-listed host receives no PHI.

**Depends on**: US1 (`call_serializer`, T021) for the full Call object.

### Tests for User Story 2 (write FIRST, confirm FAILING) ⚠️

- [X] T025 [P] [US2] Webhook-signature contract test — a faithful `retell-sdk verify()` replica accepts our signer for in-window timestamps and rejects tampered-body / wrong-key / stale-ts — in `apps/api/tests/test_compat_webhook_signature.py`
- [X] T026 [P] [US2] Integration test — call lifecycle → `call_started`/`call_ended`/`call_analyzed` delivered in `{event, call}` shape; PHI delivered only to a `COMPAT_WEBHOOK_ALLOWED_HOSTS` host (zero to a non-allow-listed host); stable `delivery_id` for dedupe — in `apps/api/tests/test_compat_webhooks.py`

### Implementation for User Story 2

- [X] T027 [P] [US2] Implement the Retell symmetric signer — `x-retell-signature: v={ts_ms},d=HMAC_SHA256(api_key, raw_body+str(ts_ms))` lowercase hex, sign-what-you-send raw bytes — in `apps/api/src/usan_api/compat/webhook_signature.py`
- [X] T028 [US2] Implement compat webhook delivery — build `{event, call}` (reuse `call_serializer`), enforce `COMPAT_WEBHOOK_ALLOWED_HOSTS` allow-list **plus** the existing `ssrf_guard`, reuse the `webhook_outbox`/poller/circuit-breaker machinery, sign with the Retell signer, inject a stable `delivery_id` — in `apps/api/src/usan_api/compat/webhook_delivery.py` (depends on T021, T027)
- [X] T029 [US2] Register/resolve the agent webhook subscription (`webhook_url` + `webhook_events`) as a compat `WebhookEndpoint` variant validated against the allow-list at registration in `apps/api/src/usan_api/compat/webhook_delivery.py`
- [X] T030 [US2] Hook call-lifecycle transitions (started/ended/analyzed) to enqueue compat deliveries for agents with a registered `webhook_url`, wired from the compat call path in `apps/api/src/usan_api/compat/routers/calls.py` / `apps/api/src/usan_api/compat/webhook_delivery.py`

**Checkpoint**: US1 + US2 both work — the CRM can place calls and reconcile outcomes from webhooks.

---

## Phase 5: User Story 3 — Manage agents & response engines over the API (Priority: P2)

**Goal**: Create/update/list/version/publish agents and the Retell-LLM they reference, via the API key.

**Independent Test**: With a key, create a Retell-LLM (get `llm_id`), create an agent referencing it,
publish a version, `list-agents` shows it alongside admin-UI agents, then place a call against it.

### Tests for User Story 3 (write FIRST, confirm FAILING) ⚠️

- [X] T031 [P] [US3] Contract + integration tests — agent & retell-llm CRUD round-trip; versioning + `publish-agent-version`; `voice_id` aliasing + unhosted-voice documented 4xx; single inventory (admin-UI + API agents both appear in `list-agents`) — in `apps/api/tests/test_compat_agents.py`

### Implementation for User Story 3

- [X] T032 [P] [US3] Implement the voice alias map (Retell `voice_id` ⇄ curated `cartesia_voice_id`) + unhosted-voice documented-error helper in `apps/api/src/usan_api/compat/voice_map.py`
- [X] T033 [P] [US3] Implement Pydantic schemas for agents (create/get/list/update + `response_engine` + `AgentResponse` echo with `compat_extras`) in `apps/api/src/usan_api/compat/schemas/agents.py`
- [X] T034 [P] [US3] Implement Pydantic schemas for retell-llm (create/get/update/list + `LlmResponse`) in `apps/api/src/usan_api/compat/schemas/retell_llm.py`
- [X] T035 [US3] Implement the agent bridge — Retell agent + response-engine ⇄ `AgentProfile`/`AgentProfileVersion` (field mapping, `llm_id`==profile facade, `compat_extras` echo, draft/publish/version-history, `model`→Vertex ignore) — in `apps/api/src/usan_api/compat/agent_bridge.py` (depends on T032, T033, T034)
- [X] T036 [US3] Implement the agents router (create→201, get, list as **bare array** + cursor params, update PATCH, delete→204, `POST /publish-agent-version/{agent_id}`) in `apps/api/src/usan_api/compat/routers/agents.py` (depends on T035)
- [X] T037 [US3] Implement the retell-llm router (create→201, get, update, delete→204, `list-retell-llms`) in `apps/api/src/usan_api/compat/routers/retell_llm.py` (depends on T035)

**Checkpoint**: US1–US3 independently functional; agents are API-provisionable.

---

## Phase 6: User Story 4 — Bulk / batch outbound calling (Priority: P2)

**Goal**: `POST /create-batch-call` (unversioned) launches many gated, tracked outbound calls.

**Independent Test**: Submit a batch with several `to_number`s + per-task variables; a Retell-shaped
batch object returns; each task lazy-upserts a Contact and is gated per-target.

**Depends on**: US1 (the number→Contact upsert shim, T022).

### Tests for User Story 4 (write FIRST, confirm FAILING) ⚠️

- [X] T038 [P] [US4] Contract + integration test — `POST /create-batch-call` (**unversioned** path) → 201 Retell-shaped batch object; per-task Contact upsert + per-target gating; assert `scheduled_timestamp` is **seconds** while `trigger_timestamp` is ms — in `apps/api/tests/test_compat_batches.py`

### Implementation for User Story 4

- [X] T039 [P] [US4] Implement Pydantic schemas for create-batch-call (`from_number`, `tasks[]` with `to_number`+vars, `trigger_timestamp` ms, `reserved_concurrency`, `call_time_window`) + the batch response (`batch_call_id`, `scheduled_timestamp` seconds, `total_task_count`) in `apps/api/src/usan_api/compat/schemas/batch.py`
- [X] T040 [US4] Implement the batch router `POST /create-batch-call` (unprefixed) — per-task number→Contact upsert (reuse T022), map tasks → `CreateBatchRequest` targets, bridge to `batches_repo.create_batch_with_targets`, return the Retell-shaped batch object — in `apps/api/src/usan_api/compat/routers/batches.py` (depends on T039); orchestration in `apps/api/src/usan_api/compat/batch_create.py` (mirrors US1's `call_create.py`; reuses the shared `upsert_contact_for_number` shim)

**Checkpoint**: US1–US4 functional; batch campaigns run through the gated/tracked path.

---

## Phase 7: User Story 5 — Supporting lookups & compatibility fidelity (Priority: P3)

**Goal**: Read-only `list-voices`/`get-voice`/`get-concurrency`, consistent id/timestamp/error fidelity,
and documented "not supported" for out-of-scope endpoints.

**Independent Test**: `list-voices`/`get-voice`/`get-concurrency` return RetellAI-shaped objects; any
out-of-scope endpoint returns `501 {status,message:"not_supported: …"}`; the same internal call always
presents the same compat id across create/get/list/webhook.

### Tests for User Story 5 (write FIRST, confirm FAILING) ⚠️

- [X] T041 [P] [US5] Tests — `list-voices`/`get-voice`/`get-concurrency` shapes; error-envelope fidelity; id consistency across create/get/list/webhook; out-of-scope endpoints → `501 not_supported` — in `apps/api/tests/test_compat_fidelity.py`

### Implementation for User Story 5

- [X] T042 [P] [US5] Implement Pydantic `VoiceResponse` + concurrency-response schemas in `apps/api/src/usan_api/compat/schemas/voices.py`
- [X] T043 [US5] Implement the catalog router (`list-voices`, `get-voice` via `voice_map`, `get-concurrency` synthesized from settings + live in-flight count) in `apps/api/src/usan_api/compat/routers/catalog.py` (depends on T032, T042)
- [X] T044 [P] [US5] Implement the out-of-scope stub router (`501 not_supported` for conversation-flow / knowledge-base / chat / web-call / voice clone-add-search / test-suite / phone-number / MCP-export-playground), documented in the compat OpenAPI, in `apps/api/src/usan_api/compat/routers/unsupported.py`

**Checkpoint**: All five user stories independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T045 [P] Verify the compat OpenAPI/docs are fully separate from the native API (gated by `COMPAT_DOCS_ENABLED`) and the native `/docs` is unchanged — `apps/api/tests/test_compat_docs.py` (disabled→404; enabled→self-contained compat OpenAPI with zero `/v1` paths; native schema excludes compat paths). Also fixed a duplicate-operation-id warning on `update-call`.
- [X] T046 [P] Confirm the rate-limit bucket covers the compat surface at the CRM's migrated throughput (FR-054) — `apps/api/tests/test_compat_ratelimit.py` (compat surface throttled by its own elevated `compat` bucket, keyed per Bearer key, separate from the operator/auth budgets; per-key isolation; passthrough when disabled)
- [~] T047 [P] **Close the contract-freeze gate**: confirm every oracle-pinned open item is locked into the contract tests (T017/T031/T038/T041), then update `docs/` and the repo README to describe the RetellAI-compatible surface and the now-resolved items (`list-retell-llms` prefix, `publish-agent-version` name, `metadata` keys, `delivery_id` location, `override_agent_version` form, `custom_sip_headers`); mark the contract "FROZEN"
  - **DONE**: README "RetellAI-compatible API" section added; every open item is encoded as an explicit `PENDING-FREEZE` marker in the contract + the contract tests.
  - **BLOCKED (cannot mark FROZEN)**: freezing requires the CRM's **captured real RetellAI traffic** (the acceptance oracle named in spec Dependencies) to pin the open shapes. Until that capture is provided, the contract stays PENDING-FREEZE by design — do not guess the shapes.
- [X] T048 Run the full pre-push gate from `apps/api`: `uv run pytest -v` (≥80% coverage), `ruff check . && ruff format --check .`, `uv run mypy` — fix to green — **GREEN**: full suite 1843 passed / 1 skipped / 0 failed; `ruff check .` + `ruff format --check .` clean (415 files); `mypy` clean (176 files). Compat-package coverage **89%** (measured via ephemeral `uv run --with pytest-cov`; the repo CI does not install coverage tooling).
- [~] T049 Run [quickstart.md](./quickstart.md) scenarios 1–8 against a local `make up` stack and confirm each expected outcome
  - **DONE**: the automated equivalent of every scenario passes in the suite (`test_compat_auth`/`_calls`/`_calls_integration`/`_webhooks`/`_agents`/`_batches`/`_fidelity`/`_mount_isolation`/`_rls_isolation`).
  - **DEFERRED (operator)**: the live `make up` + **real-telephony** legs of scenarios 2 (a real outbound dial) and 4 (real webhook delivery + `Retell.verify`) need real Telnyx/LiveKit and a running stack — the standing manual live-call validation step, same as prior plans.
- [X] T050 [P] Audit-log review: every PHI-touching compat endpoint emits a structured org+key-id audit line and no token/PHI leaks into logs or the catch-all 500 handler (Constitution II/VI) — reviewed every compat `logger` call (routers bind org+op+opaque-id only; webhook delivery never logs the body/URL; auth binds key-id not the token; validation 422 names only the field, not the value; catch-all 500 logs the exception type name only + returns a fixed envelope). Locked in by `apps/api/tests/test_compat_audit.py`.

---

## Dependencies & Execution Order

### Phase dependencies
- **Setup (P1)** → no deps.
- **Foundational (P2)** → depends on Setup; **BLOCKS all user stories**.
- **User Stories (P3–P7)** → all depend on Foundational. US2 and US4 additionally reuse US1 artifacts
  (`call_serializer` T021 / the upsert shim T022); US1 is the natural first.
- **Polish (P8)** → depends on the desired stories being complete.

### Story dependencies
- **US1 (P1)** — after Foundational; no story deps. **MVP**.
- **US2 (P1)** — after Foundational; reuses US1's `call_serializer` (T021).
- **US3 (P2)** — after Foundational; independent (agents can also be created in the admin UI).
- **US4 (P2)** — after Foundational; reuses US1's number→Contact shim (T022).
- **US5 (P3)** — after Foundational; the voice catalog (T032, in US3) is reused by `get-voice` — if US5
  ships before US3, pull T032 forward into Foundational.

### Within each story
- Tests FIRST (RED) → schemas/models → serializers/bridges → routers → audit/integration.

---

## Parallel Opportunities

- **Setup**: T002, T003 in parallel (after T001).
- **Foundational**: tests T004–T006 in parallel; impl T007, T008, T009, T010, T016 in parallel; then
  T011→T012→T013/T014→T015 in order.
- **US1**: T017, T018 (tests) in parallel; T019, T020 in parallel; then T021→T022→T023→T024.
- **US3**: T032, T033, T034 in parallel → T035 → T036, T037.
- Across teams: once Foundational lands, US1/US3/US5 can proceed in parallel; US2 follows US1's T021 and
  US4 follows US1's T022.

### Parallel Example: User Story 1

```bash
# Tests first (must fail):
Task: "Contract tests for /v2,/v3 calls in apps/api/tests/test_compat_calls.py"          # T017
Task: "Integration tests for call lifecycle in apps/api/tests/test_compat_calls_integration.py"  # T018
# Then parallel building blocks:
Task: "Status map in apps/api/src/usan_api/compat/status_map.py"                          # T019
Task: "Call schemas in apps/api/src/usan_api/compat/schemas/calls.py"                     # T020
```

---

## Implementation Strategy

### MVP first (US1 only)
1. Phase 1 Setup → 2. Phase 2 Foundational (CRITICAL) → 3. Phase 3 US1 → **STOP & validate** (place a
real call, get/list it, confirm DNC/quiet-hours 400). Deploy/demo the MVP.

### Incremental delivery
US1 (calls) → US2 (webhooks, completing the event-driven loop) → US3 (agent config) → US4 (batch) →
US5 (lookups + fidelity). Each is an independently testable increment that does not break the prior.

### Parallel team strategy
After Foundational: Dev A → US1 (then US2), Dev B → US3, Dev C → US5; US4 picks up once US1's T022 lands.

---

## Notes

- `[P]` = different files, no incomplete-task dependency.
- Tests are written first and confirmed FAILING (Constitution IV); fix implementation, not tests.
- The entire surface is additive under `apps/api/src/usan_api/compat/` — the native `/v1` plane and
  `services/agent` are never modified beyond the three wiring edits (`main.py`, `settings.py`,
  `ratelimit.py`) and are guarded by the mount-isolation tests (T005).
- Several exact RetellAI shapes are flagged "pin against the captured oracle" in the contracts; lock
  those from the CRM's real traffic before freezing the contract tests.
- Commit after each task or logical group; run `ruff` + `mypy` locally (CI runs mypy — [[ci_runs_mypy]]).
- Test-file convention: **contract** tests and **integration** tests live in separate files per area —
  contract in `test_compat_<area>.py` (e.g. T017 `test_compat_calls.py`), integration in
  `test_compat_<area>_integration.py` (e.g. T018 `test_compat_calls_integration.py`); likewise
  `test_compat_webhooks.py` (T026) and `test_compat_fidelity.py` (T041) are additive to the plan's sketch.
