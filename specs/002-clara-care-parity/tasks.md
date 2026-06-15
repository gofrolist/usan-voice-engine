---
description: "Task list for Clara Care Parity"
---

# Tasks: Clara Care Parity — Closing the RetellAI Behavioral Gap

**Input**: Design documents from `specs/002-clara-care-parity/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — the constitution makes Test-First Development NON-NEGOTIABLE (Principle IV). Every story writes failing tests (RED) before implementation (GREEN), ≥80% coverage.

**Organization**: Tasks grouped by user story (US1–US8 from spec.md) in priority order, each an independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no incomplete dependencies)
- File paths are repo-relative. `apps/api/src/usan_api/` = api; `services/agent/src/usan_agent/` = agent.

## Conventions (from plan.md / research.md)

- Migrations: `apps/api/migrations/versions/NNNN_*.py`, sequential after `0016`, chained `down_revision`. Additive/backward-compatible.
- New agent tools: function in `check_in.py` `_TOOL_REGISTRY` + a `noop_*` in `_TEST_TOOL_REGISTRY` + `api_client` method + `/v1/tools/*` endpoint. Every new builtin var has a default in the agent `prompt_vars` mirror (parity test).
- PHI: summarization/reports via Vertex AI (ADC); family SMS PHI-minimized; never the Gemini Developer API.

---

## Phase 1: Setup (Shared)

- [x] T001 Verify clean baseline: confirm latest migration is `0016`, run `cd apps/api && uv run pytest` and `cd services/agent && uv run pytest` green, plus `ruff check` + `uv run mypy` in both, before any changes.
- [x] T002 [P] Add new config to `apps/api/src/usan_api/settings.py`: poller flags (`callback_dialer_poller_enabled`, `notification_outbox_enabled`, `family_report_poller_enabled`), `telnyx_inbound_public_key` (SecretStr), `med_reask_cap` (int, default 3), Spanish-profile id; add blank-to-None validators; cover in `apps/api/tests/test_settings.py`.
- [x] T003 [P] Record the Telnyx-messaging-BAA confirmation requirement (research Decision 9) and PHI-minimized-SMS rule in `docs/` as a go-live compliance gate.

---

## Phase 2: Foundational (Outbound-notification substrate) — BLOCKS US1, US2, US7, US8

**Purpose**: the PHI-minimized family/operator SMS channel that crisis alerts (US1 MVP), missed-call/crisis alerts (US2), opt-out acks (US7), and reports (US8) all depend on.

- [x] T004 [P] Test (RED) for PHI-minimized notification builder (no clinical content; correct templates per kind) in `apps/api/tests/test_notifications.py`.
- [x] T005 [P] Test (RED) for the notification outbox poller (claims `call_id IS NULL` pending rows, `dedupe_key` idempotency, per-row commit) in `apps/api/tests/test_notification_outbox.py`.
- [x] T006 Migration `0017_notifications_substrate.py`: `sms_messages.call_id` → nullable; add `kind` (CHECK `in_call|family_alert|family_report|opt_out_ack`, default `in_call`); add `dedupe_key` (nullable, unique-where-not-null); `template_key` → nullable.
- [x] T007 Extend `SmsMessage` model in `apps/api/src/usan_api/db/models.py` + `apps/api/src/usan_api/repositories/sms_messages.py` for nullable `call_id`, `kind`, `dedupe_key`.
- [x] T008 Implement `apps/api/src/usan_api/notifications.py`: PHI-minimized builder that creates `sms_messages` rows (kind/dedupe_key) for family alerts/reports/opt-out acks.
- [x] T009 Implement `apps/api/src/usan_api/notification_outbox.py` poller phase (flush `call_id IS NULL` pending via `telnyx_messaging.send_sms`, dedupe, per-row commit); wire into app lifespan behind `notification_outbox_enabled`.

**Checkpoint**: family/operator SMS can be enqueued and delivered idempotently.

---

## Phase 3: User Story 1 — Crisis detection & emergency escalation (P1) 🎯 MVP

**Goal**: detect the five crisis categories (LLM + deterministic safety net), speak the correct resource, raise an urgent escalation, and alert family.

**Independent Test**: scripted calls per category produce the right resource + an urgent flag + a family alert; the safety net fires even when the LLM is stubbed not to; benign control set stays ≤2% false escalation.

### Tests (RED first)

- [x] T010 [P] [US1] Unit test for the deterministic `crisis_watcher` matcher incl. the benign control set (≤2% FP, SC-002) in `services/agent/tests/test_crisis_watcher.py`.
- [x] T011 [P] [US1] Contract test for `POST /v1/tools/raise_crisis` (per-category resource, idempotent per call+category) in `apps/api/tests/test_tools_raise_crisis.py`.
- [x] T012 [P] [US1] Integration test: crisis → `urgent` flag with crisis columns + family alert enqueued in `apps/api/tests/test_crisis_escalation.py`.

### Implementation

- [x] T013 [US1] Migration `0018_followup_crisis_cols.py`: add `crisis_category`, `detection_source`, `resource_offered`, `family_notified` to `follow_up_flags`.
- [x] T014 [US1] Extend `FollowUpFlag` model + `apps/api/src/usan_api/repositories/follow_up_flags.py` (crisis write/read; upsert per `(call_id, category)`; set `family_notified`).
- [x] T015 [P] [US1] `apps/api/src/usan_api/emergency_resources.py` code catalog (988 / 911 / Adult Protective Services / Poison Control 1-800-222-1222 + spoken scripts).
- [x] T016 [P] [US1] `apps/api/src/usan_api/schemas/crisis.py` (RaiseCrisisRequest/Response).
- [x] T017 [US1] Implement `POST /v1/tools/raise_crisis` in `apps/api/src/usan_api/routers/tools.py`: record escalation, return resource, enqueue crisis family alert via `notifications.py` (dedupe `crisis:{flag_id}`).
- [x] T018 [P] [US1] Agent `services/agent/src/usan_agent/crisis_watcher.py`: deterministic STT phrase matcher mirroring `voicemail.py`.
- [x] T019 [US1] Agent: add `raise_crisis` to `_TOOL_REGISTRY` + `noop_raise_crisis` in `_TEST_TOOL_REGISTRY` (`check_in.py`) + `api_client.raise_crisis`.
- [x] T020 [US1] Agent `worker.py`: subscribe `crisis_watcher` on `user_input_transcribed`; on match call `raise_crisis` and deliver the resource via `session.say`.
- [x] T021 [US1] Author crisis-handling + resource-delivery guidance in the agent profile prompt (so the LLM path also calls `raise_crisis`).

**Checkpoint**: US1 fully functional and independently testable (MVP).

---

## Phase 4: User Story 2 — Family task relay & alerting (P2)

**Goal**: family texts a task → conveyed next call → closed; family alerted on missed calls and crises.

**Independent Test**: register a contact, POST a signed inbound SMS task, run the next call (task spoken then closed, not repeated); force a missed call and a crisis and confirm deduped family alerts.

**Depends on**: Foundational (notifications). The inbound webhook infra built here is also reused by US7.

### Tests (RED first)

- [x] T022 [P] [US2] Contract test for `POST /v1/webhooks/telnyx` (signature verify, msg-id idempotency, routing) in `apps/api/tests/test_telnyx_inbound.py`.
- [x] T023 [P] [US2] Integration test: family task loop (intake → convey → close → no repeat) in `apps/api/tests/test_family_tasks.py`.
- [x] T024 [P] [US2] Integration test: missed-call + crisis family alerts delivered and deduped in `apps/api/tests/test_family_alerts.py`.
- [x] T089 [P] [US2] Test (RED) asserting family-alert delivery latency target (SC-004): the notification outbox claims + dispatches a pending alert within the 5-minute budget (poller interval), in `apps/api/tests/test_notification_outbox.py`.

### Implementation

- [x] T025 [US2] Migration `0019_family_contacts_tasks.py`: `family_contacts` + `family_tasks` tables (per data-model.md).
- [x] T026 [P] [US2] `FamilyContact`/`FamilyTask` models + `repositories/family_contacts.py` + `repositories/family_tasks.py`.
- [x] T027 [P] [US2] `schemas/family.py` + `schemas/inbound_sms.py`.
- [x] T028 [US2] `apps/api/src/usan_api/telnyx_inbound.py`: signature/timestamp verifier mirroring `webhook_signing`/`livekit_webhooks`.
- [x] T029 [US2] `POST /v1/webhooks/telnyx` in `routers/webhooks.py`: verify → family-task intake; `needs_safety_review` for unsafe tasks; unmatched-sender safe default; idempotent on Telnyx msg id.
- [x] T030 [US2] `builtin_vars.py`: resolve `open_family_tasks`; add default in agent `prompt_vars.py` mirror.
- [x] T031 [US2] `close_family_task` tool: `/v1/tools/close_family_task` endpoint + agent tool + noop.
- [x] T032 [US2] `routers/admin_family.py`: family-contacts CRUD + family-tasks list/patch (approve `needs_review`/close).
- [x] T033 [US2] Missed-call alert: at call finalization (retries exhausted/missed) enqueue a `family_alert` via `notifications.py` (dedupe `missed:{call_id}`).
- [x] T034 [US2] Verify crisis-alert wiring from US1 sets `family_notified` and respects contact `alert_prefs`.
- [x] T088 [US2] Operator-fallback recipient (FR-013): when an elder has no family contact, `notifications.py` routes alerts/reports to the operator queue and surfaces the absence to operators; add test `apps/api/tests/test_notifications_fallback.py`.

**Checkpoint**: US1 + US2 both work independently.

---

## Phase 5: User Story 3 — Medication adherence with re-reminders (P2)

**Goal**: not-taken meds open a re-reminder, re-asked next touch, cleared on confirmation, capped without nagging.

**Independent Test**: log a med not-taken → reminder pending → re-asked next touch → confirm clears; drive to cap → routine flag, no further nag.

### Tests (RED first)

- [x] T035 [P] [US3] Contract test: `log_medication` not-taken opens reminder, taken clears, in `apps/api/tests/test_medication_reminders.py`.
- [x] T036 [P] [US3] Integration test: re-ask delivered next touch; cap → routine `follow_up_flags`, no nag, in `apps/api/tests/test_medication_reminders_flow.py`.

### Implementation

- [x] T037 [US3] Migration `0020_medication_reminders.py`: `medication_reminders` table + partial unique (one `pending` per `(elder_id, medication_name)`).
- [x] T038 [P] [US3] `MedicationReminder` model + `repositories/medication_reminders.py`.
- [x] T039 [US3] Extend `POST /v1/tools/log_medication` in `routers/tools.py`: open/refresh reminder on `taken=false`; clear on `taken=true`; cap → routine `follow_up_flags`.
- [x] T040 [US3] `builtin_vars.py`: resolve `pending_med_reasks`; add default in agent `prompt_vars.py` mirror.
- [x] T041 [US3] Author medication re-ask guidance in the profile prompt.

**Checkpoint**: US3 independently functional.

---

## Phase 6: User Story 4 — Personalized memory across calls (P2)

**Goal**: structured personal facts + prior-call summary/plans carried forward and used naturally; new facts captured.

**Independent Test**: seed facts; run call 1 (state a fact + a plan) → summary + extracted fact written; run call 2 → prompt carries summary/plans/facts/dates and agent references one.

### Tests (RED first)

- [x] T042 [P] [US4] Contract test for `POST /v1/tools/record_personal_fact` in `apps/api/tests/test_tools_personal_fact.py`.
- [x] T043 [P] [US4] Test: post-call summarization writes a summary + extracts facts (Vertex mocked) in `apps/api/tests/test_summarization.py`.
- [x] T044 [P] [US4] Integration test: memory builtins carried into the next call in `apps/api/tests/test_memory_carryforward.py`.

### Implementation

- [x] T045 [US4] Migration `0021_personal_facts_summaries.py`: `personal_facts` + `conversation_summaries` tables.
- [x] T046 [P] [US4] `PersonalFact`/`ConversationSummary` models + `repositories/personal_facts.py` + `repositories/conversation_summaries.py`.
- [x] T047 [P] [US4] `schemas/personalization.py`.
- [x] T048 [US4] `record_personal_fact` tool: endpoint + agent tool + noop (also `api_client` + both `check_in` registries + `tool_catalog`).
- [x] T049 [US4] `apps/api/src/usan_api/summarization.py`: Vertex (ADC) post-call summary + fact extraction (PHI-safe, reuses `vertex_test.run_vertex_turn`).
- [x] T050 [US4] Trigger summarization on `call.completed` (both `end_call` tool + `room_finished` webhook; flag-gated, idempotent per call).
- [x] T051 [US4] `builtin_vars.py`: resolve `personal_facts`, `last_call_summary`, `open_plans`, `important_dates` (+ `build_memory_params`); carried at all 3 sites; mirrored in `variable_catalog`/`prompt_substitution`/agent `prompt_vars`; woven into both default prompts.

**Checkpoint**: US4 independently functional; verify no PHI leaves Vertex/Postgres (SC-013).

---

## Phase 7: User Story 5 — Evening calls & schedule flexibility (P2)

**Goal**: independent, toggleable morning + evening call windows per elder.

**Independent Test**: two slots → two calls on enabled days; disable evening → only morning; DNC → neither.

### Tests (RED first)

- [x] T052 [P] [US5] Test: per-slot materialization (both), evening-disable suppresses evening, DNC blocks both, in `apps/api/tests/test_schedule_slots.py`.

### Implementation

- [x] T053 [US5] Migration `0022_schedule_slot.py`: add `call_schedules.slot` (CHECK `morning|evening`, default `morning`), relax `UNIQUE(elder_id)` → `UNIQUE(elder_id, slot)`, backfill `morning`.
- [x] T054 [US5] Update `CallSchedule` model + `repositories/call_schedules.py` (`get_by_elder` → list; per-slot create/patch).
- [x] T055 [US5] `schedule_orchestrator.py`: iterate slots in materialization (per-slot window/days; keep `sched:` idempotency key now per slot row).
- [x] T056 [US5] `routers/schedules.py` + schemas: `slot` on create/patch/list; 409 only on duplicate `(elder_id, slot)`.

**Checkpoint**: US5 independently functional.

**US5 COMPLETE** ✅ (evening calls & schedule flexibility) — 2026-06-15

- **T053** migration `0022`: `call_schedules.slot` TEXT `DEFAULT 'morning'` + `ck_call_schedules_slot` CHECK(morning|evening); dropped inline `call_schedules_elder_id_key`, added `uq_call_schedules_elder_slot UNIQUE(elder_id, slot)`; existing rows backfill to `morning` in place. Raw-SQL style matching this module.
- **T054** `CallSchedule.slot` column (inline `unique=True` on `elder_id` removed — composite unique is migration-owned); repo `create_schedule(slot='morning')`, `get_by_elder` → `list` (the old `scalar_one_or_none()` would now raise `MultipleResultsFound` for a two-slot elder), new `get_by_elder_slot`, `list_schedules` slot filter.
- **T055** NO orchestrator change needed: each slot is its own `call_schedules` row with its own `next_run_at`/window/`enabled` and its own `sched:{schedule.id}:{day}` key, so phase-3 already materializes slots independently. Proven by `test_schedule_slots.py`.
- **T056** schemas: `Slot = Literal["morning","evening"]`; `CreateScheduleRequest.slot` (default `morning`), `ScheduleResponse.slot: Slot`; `UpdateScheduleRequest` `extra="forbid"` (slot is immutable identity → move = delete+create). Router: per-`(elder, slot)` 409 via `get_by_elder_slot` (+ IntegrityError race fallback), `?slot=` list filter typed `Slot | None` (422 on unknown).

**Adversarial review** (4 lenses → per-finding refutation): 0 CRITICAL / 0 HIGH; **1 MEDIUM + 5 LOW confirmed**, all addressed:
- MEDIUM — daily-cap coupling: a two-slot elder consumes the default cap (2), and `cap=1` would skip the evening daily. Resolved as a *documented, tested contract* (not a hard floor, which would forbid a valid single-slot `cap=1` and break `test_materializer`): `settings.py` comment documents the total-roots-across-slots coupling; `test_schedule_slots.py::test_daily_cap_bounds_total_roots_across_slots` pins it (morning dials, evening `skipped_daily_cap`, stays enabled, retries next day — fail-closed, observable).
- LOW — PATCH silently ignored `slot` → `extra="forbid"` (mirrors `custom_variables`) + 422 tests.
- LOW (×2, dup) — `?slot=` accepted unknown values → typed `Slot | None` (422) + test.
- LOW — `ScheduleResponse.slot` was bare `str` → tightened to `Slot`.
- LOW — `get_by_elder` "dead code": **kept** — the list conversion was mandatory (avoids `MultipleResultsFound`); it's a legitimate slot-ordered accessor, tested.

Final US5 snapshot: apps/api **1441 passed / 1 skipped**, `mypy` clean (125 files), `ruff` check+format clean; services/agent untouched (US5 is API-only — no Principle I cross-import). Migrations single head `0022`.

---

## Phase 8: User Story 6 — Monthly survey & mood-boosting activities (P3)

**Goal**: once-monthly wellbeing survey; non-repeating mood-boosting activity on low mood.

**Independent Test**: survey-due elder → survey recorded once/month; two low-mood calls → different activities until catalog exhausted.

### Tests (RED first)

- [x] T057 [P] [US6] Contract test: `record_survey` (once/month unique) + `get_activity` (LRU non-repeat) in `apps/api/tests/test_survey_activity.py`.
- [x] T058 [P] [US6] Integration test: `survey_due` flag + non-repeating activity sequence in `apps/api/tests/test_wellbeing_flow.py`.

### Implementation

- [x] T059 [US6] Migration `0023_survey_activity.py`: `wellbeing_survey_results` (unique `(elder_id, period_month)`) + `activity_history`.
- [x] T060 [P] [US6] `WellbeingSurveyResult`/`ActivityHistory` models + `repositories/survey_results.py` + `repositories/activity_history.py`.
- [x] T061 [P] [US6] `apps/api/src/usan_api/activities_catalog.py` code catalog (breathing/memory/game entries + scripts).
- [x] T062 [US6] `record_survey` + `get_activity` tools: endpoints (LRU selection in `get_activity`) + agent tools + noops.
- [x] T063 [US6] `builtin_vars.py`: resolve `survey_due`; add default in agent `prompt_vars.py` mirror.
- [x] T064 [US6] Author survey + low-mood activity guidance in the profile prompt.

**Checkpoint**: US6 independently functional.

**US6 COMPLETE** ✅ (monthly survey & mood-boosting activities)

- Per-slot data model: each `(elder, period_month)` survey is unique → `record_survey` is
  once-per-month idempotent (ON CONFLICT returns existing, never 409); `get_activity` selects
  over a CODE catalog (`activities_catalog.py`, 2×{breathing,memory,game}) using a pure LRU
  policy — exclude (used ≤30d) ∪ (last 3 used), prefer never-used, fall back to least-recently-used
  when exhausted (FR-034 / SC-009). `survey_due` builtin wired through BOTH call paths (outbound +
  inbound, unknown caller → false). Agent gained `record_survey`/`get_activity` tools + noops; the
  wellbeing step authored into both prompt copies (T064).
- Mirror lockstep verified: `survey_due` present in all 4 builtin mirrors (variable_catalog,
  prompt_substitution, agent prompt_vars, builtin_vars DATA_BUILTIN_NAMES); 12-tool catalog ==
  agent `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY`; default-enabled lists agree api↔agent.
- PHI: survey scores are PHI (never logged); `survey_due` is a non-PHI scheduling flag (phi=False).
  Principle I held — agent keeps its own mirrors, no cross-import.
- Post-implementation adversarial review (5 lenses: migration/model, selection-policy, repo/endpoint,
  parity, spec/constitution; 23 agents, 3-skeptic refutation): **0 genuine defects**. The 3 "confirmed"
  HIGH/MED findings were a single FALSE POSITIVE — this env's text display strips parens from
  `except (A,B,C):`, so reviewers (and their verifiers, same Read path) saw Python-2 syntax that
  isn't in the bytes. Disproven by ground truth: `py_compile` exit 0, `ast` shows `ExceptHandler.type`
  is a 3-tuple, `ruff`/`mypy`/imports/1455 tests all pass (see memory `except-paren-display-artifact`).
- Final US6 snapshot: apps/api **1455 passed / 1 skipped**, `mypy` clean (129 files), `ruff`
  check+format clean; services/agent **253 passed**, mypy + ruff clean. Migrations single head `0023`.

---

## Phase 9: User Story 7 — Anti-scam education & opt-out (P3)

**Goal**: scam red-flag warnings; honor opt-out via spoken request and inbound STOP.

**Independent Test**: describe a scam → Clara warns + explains red flags; say "stop calling" and text STOP → both add DNC, no future outbound.

**Depends on**: US2 inbound webhook infra (`telnyx_inbound.py`, `/v1/webhooks/telnyx`) + Foundational notifications.

### Tests (RED first)

- [x] T065 [P] [US7] Contract test: `register_opt_out` → DNC add + notify, in `apps/api/tests/test_tools_opt_out.py`.
- [x] T066 [P] [US7] Test: inbound `STOP` → DNC add + `opt_out_ack`, in `apps/api/tests/test_inbound_stop.py`.

### Implementation

- [x] T067 [US7] `register_opt_out` tool: endpoint + agent tool + noop (DNC add via `dnc_repo` + `lock_phone`; enqueue `opt_out_ack` + operator notify).
- [x] T068 [US7] Extend `telnyx_inbound` routing in `routers/webhooks.py`: detect opt-out keywords (STOP/STOPALL/UNSUBSCRIBE/CANCEL/END/QUIT) BEFORE task intake → DNC + `opt_out_ack`.
- [x] T069 [US7] Author anti-scam red-flag guidance in the profile prompt.
- [x] T087 [US7] Informational SMS on request (FR-041): extend the `send_sms` template set / tool so the agent can send the elder an SMS with helpful information and relevant phone numbers — including emergency resource numbers sourced from `emergency_resources.py` — on request; keep it PHI-minimized. Tests in `apps/api/tests/test_tools_send_sms_info.py`.

**Checkpoint**: US7 independently functional.

**US7 COMPLETE** ✅ (anti-scam education & opt-out) — 2026-06-15

- ✅ Opt-out (FR-037/038/039 + SC-010): `register_opt_out` tool (spoken) — `dnc_repo.lock_phone` + `add_entry` (suppresses future outbound, terminal-at-birth `DNC_BLOCKED`), idempotent `enqueue_opt_out_ack` (`opt_out:{call_id}`), and `ensure_opt_out_flag` (routine `operator_alert` operator-queue entry, ensure-once). Inbound `STOP`: `is_opt_out_keyword` (STOP/STOPALL/UNSUBSCRIBE/CANCEL/END/QUIT, normalized) intercepts in `telnyx_webhook` BEFORE family-task intake → unconditional DNC add + (known elder) `opt_out_ack` (`opt_out_sms:{message_id}`). Both ack bodies PHI-free (Principle II); both numbers canonicalized via `to_e164` so the DNC key matches the outbound gate.
- ✅ Anti-scam (FR-036, T069): scam-awareness + opt-out + info-SMS guidance woven into BOTH default prompt blocks (outbound checkin + inbound), mirrored byte-identically in apps/api and services/agent.
- ✅ Informational SMS (FR-041, T087): `send_info_sms` tool — PHI-free helpful-numbers body from `emergency_resources.informational_sms_body()` (911 / 988 / Poison Control / Eldercare, single-sourced from the crisis catalog), shares the per-call `MAX_SMS_PER_CALL` budget, post-call outbox delivery; no template needed (unlike send_sms).
- ✅ Catalog/registry lockstep: 12→14 tools (`tool_catalog`), agent `_TOOL_REGISTRY` + `_TEST_TOOL_REGISTRY` + both default `enabled` lists; api↔agent client + noop mirrors. Principle I held (no cross-import). No DB migration (reuses `dnc_list` / `sms_messages` opt_out_ack / `follow_up_flags` operator_alert) → head stays `0023`.
- ✅ Tests (RED→GREEN): `test_tools_opt_out.py` (6: DNC+ack+flag, idempotent, SC-010 suppression, 403 token-scope, 409 no-elder), `test_inbound_stop.py` (keyword unit cases + 6 webhook: STOP→DNC+ack, all 6 keywords, no-task-on-STOP, non-keyword still routes, unknown-sender DNC-no-ack, redelivery idempotent), `test_tools_send_sms_info.py` (5: catalog-sourced body, PHI-free, budget, 403, 409), T069 prompt-guidance pin (both sides); catalog/runtime/config pins bumped 12→14.
- ✅ Post-implementation adversarial review (4 lenses: correctness, PHI/security, idempotency, parity; 25 agents, 3-skeptic refutation): **0 CRITICAL, 1 genuine fix, rest dismissed via ground truth**. FIXED: HIGH — `_route_inbound_family_task` (pre-existing US2 code) matched the raw sender against E.164 contacts; normalized via `to_e164` to parity with the opt-out path (FR-008/014) + regression test for a 10-digit national sender. Dismissed: `ensure_opt_out_flag` "race" — its sole caller `register_opt_out` holds the per-phone `pg_advisory_xact_lock` across the whole transaction incl. the flag create, so concurrent same-elder calls serialize (verifiers analyzed the repo fn in isolation; one Bash-injected demo test was reverted); `ensure_operator_missed_flag` race — pre-existing US2 code, poller/claim-serialized, out of scope; cross-path ack dedup — distinct opt-out channels, elder-facing ack already atomic on its unique `dedupe_key`, benign. The recurring "Python-2 except syntax" CRITICAL (env paren-stripping display artifact, see memory `except-paren-display-artifact`) was correctly refuted 0/3 this round thanks to the ground-truth instruction in the review prompt.
- Final US7 snapshot: apps/api **1493 passed / 1 skipped**, `mypy` clean (129 files), `ruff` check+format clean; services/agent **253 passed**, mypy + ruff clean. Migrations single head `0023` (no new migration).

---

## Phase 10: User Story 8 — Callback auto-dial, Spanish callback, monthly family report (P3)

**Goal**: requested callbacks auto-dialed (quiet-hours/DNC honored); Spanish callers get a scheduled Spanish callback; monthly family report delivered.

**Independent Test**: request a callback in 1h → call placed at clamped time; quiet-hours request → deferred; speak Spanish → Spanish callback dialed; monthly job → report row + PHI-minimized family SMS.

**Depends on**: Foundational notifications (report SMS); US2 family contacts (report recipient).

### Tests (RED first)

- [x] T070 [P] [US8] Test: callback dialer claims due request → quiet-hours clamp + DNC + idempotent `create_call` (`callback:{id}`) in `apps/api/tests/test_callback_dialer.py`.
- [x] T071 [P] [US8] Contract test: `set_spanish_callback` sets `meta["language"]` + creates Spanish callback in `apps/api/tests/test_tools_spanish_callback.py`.
- [x] T072 [P] [US8] Test: monthly report generation + PHI-minimized family SMS in `apps/api/tests/test_family_report.py`.

### Implementation

- [x] T073 [US8] Migration `0024_callback_autodial.py`: widen `callback_requests.status` CHECK (+`scheduled`,`dialed`) + add `dispatched_call_id` (and `profile_override` for the Spanish callback).
- [x] T074 [US8] `repositories/callback_requests.py`: `open → scheduled → dialed` transitions (`list_due_open_ids`/`claim_open_for_dial`/`mark_scheduled`/`reconcile_dialed`).
- [x] T075 [US8] `apps/api/src/usan_api/callback_dialer.py` poller phase: claim due `open` callbacks → `quiet_hours.next_allowed` clamp → DNC → `calls_repo.create_materialized_root` (idempotency `callback:{id}`, `scheduled_at`); wired behind `callback_dialer_poller_enabled`.
- [x] T076 [US8] `set_spanish_callback` tool: endpoint + agent tool + noop (set `elders.meta["language"]="es"`; create callback with Spanish `profile_override`). 15th catalog tool, api↔agent in lockstep.
- [x] T077 [US8] Migration `0025_family_reports.py`: `family_reports` table.
- [x] T078 [US8] `FamilyReport` model + `repositories/family_reports.py`; `apps/api/src/usan_api/family_report_job.py` monthly poller (aggregate trends + Vertex narrative w/ deterministic fallback + enqueue PHI-minimized SMS); wired behind `family_report_poller_enabled`.
- [x] T079 [US8] Admin reads in `routers/admin_tools.py` (callback-requests `scheduled`/`dialed`/`dispatched_call_id`) + `routers/admin_family.py` (family-reports list/resend).

**Checkpoint**: all user stories independently functional.

---

## Phase 11: Polish & Cross-Cutting

- [x] T080 [P] api↔agent parity contract test covering ALL new tools + builtins (defaults present in agent mirror) across `apps/api/tests/` + `services/agent/tests/`. DONE: new `test_api_agent_tool_parity.py` (api side, AST-reads the agent `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY` + every `@function_tool` callable, importlib-loads the agent default `enabled`; compares to API `TOOL_NAMES`); builtins/prompt-bodies via existing `test_prompt_substitution_parity.py`; agent side self-pins (`test_check_in.py`/`test_agent_config.py`). Drift-detection proven (synthetic miss fails).
- [x] T081 [P] Ensure ≥80% coverage in both units; `ruff check`, `ruff format`, `uv run mypy` clean (CI gate). DONE: apps/api **95%** (1524 passed/1 skipped), services/agent **88%** (253 passed); ruff + ruff format + mypy clean in both.
- [x] T082 [P] Structured logging + `admin_audit_log` entries for new mutations (crisis raised, opt-out, callback materialized, family SMS sent, summary written); assert no PHI in logs/audit/webhook payloads. DONE: webhook payload PHI-exclusions pinned (`test_webhook_events.py::test_phi_exclusions_pinned`, `PHIPHI_*` sentinels incl. callback time-text/notes); admin-plane mutations record PHI-free audit detail (filter-shape/counts only); pollers log exception TYPE only. Added `test_admin_family` coverage for the family-report resend audit (`detail == {"recipients": N}`, no name/phone).
- [x] T083 Compliance gate (CRITICAL, constitution Principle II — blocks family-SMS go-live): confirm Telnyx **messaging** is BAA-covered before any family-SMS feature ships; add a test asserting family SMS bodies contain no clinical content (PHI-minimized); if BAA cannot be confirmed, keep family-SMS features (US2 alerts, US8 report, US7 opt-out ack) behind their poller flags (disabled) until resolved. ENGINEERING DONE: no-clinical-content tests exist (`test_notifications.py`, `test_family_report.py`, `test_admin_family.py`); ship-disabled fallback verified (`test_clara_parity_settings_defaults` + `test_outbox_leaves_pending_when_messaging_disabled` — defense-in-depth: `TELNYX_MESSAGING_ENABLED=false` transmits nothing). ⛔ HUMAN GATE STILL OPEN: the written Telnyx-Messaging-BAA confirmation (`docs/compliance/clara-care-parity-go-live.md` Gate 1 checkbox) is an operator sign-off, not a code task.
- [x] T084 [P] Update `docs/` + profile prompt templates for the new behaviors; document new env flags. DONE: new flags plumbed through `docker-compose.yml` api env + documented in `.env.example` + `.env.prod.example` (locked by `test_infra_clara_parity_env.py`); compliance doc updated; profile-prompt guidance (SPANISH/activity/opt-out) already lives in `DEFAULT_AGENT_CONFIG` (both mirrors).
- [ ] T085 Run all `quickstart.md` scenarios 1–8 end-to-end against the local stack. ⚠️ OPERATOR-GATED: every scenario's LOGIC is covered by the automated suite (crisis_watcher, family tasks, med re-asks, memory, schedule slots, survey/activity, opt-out, callback dialer, family report), but the true live end-to-end needs `make up` + real Telnyx telephony + live Vertex/Cartesia (a placed/received phone call) — not runnable in this environment. Runbook is `quickstart.md`.
- [ ] T086 Add new secret keys (Telnyx inbound key, poller flags) to the VM `.env` via IAP-SSH BEFORE cutting the deploy tag (deploy procedure). ⚠️ OPERATOR-GATED (production + deploy-time): refresh `/opt/usan/infra/.env` with `TELNYX_INBOUND_PUBLIC_KEY` + `SPANISH_PROFILE_ID` (+ any poller flag to enable) via IAP-SSH BEFORE the `v*` tag deploy (the tag deploy never re-fetches secrets). Compose plumbing is verified ready (`docker compose config` renders all 8 keys, inert). Requires explicit operator action; not done autonomously.

---

## Dependencies & Execution Order

### Phase dependencies
- **Setup (P1)** → no deps.
- **Foundational (P2)** → depends on Setup; BLOCKS US1, US2, US7, US8 (notification substrate).
- **US1 (P3)** → Foundational. MVP.
- **US2 (P4)** → Foundational. Builds inbound webhook infra reused by US7.
- **US3, US4, US5 (P5–P7)** → depend only on Setup + their own migrations; independent of US1/US2 (they do NOT need the notification substrate). Can run in parallel with US1/US2 if staffed.
- **US6 (P8)** → independent (Setup only).
- **US7 (P9)** → Foundational + US2 inbound webhook infra.
- **US8 (P10)** → Foundational (report SMS) + US2 (family recipient).
- **Polish (P11)** → after the desired stories.

### Within each story
Tests (RED) → migration → model/repo → schema → endpoint/tool → agent-side → prompt/integration.

### Parallel opportunities
- Setup T002/T003 in parallel.
- Foundational tests T004/T005 in parallel; then T006→T007→T008→T009 sequential (shared files).
- All `[P]` tests within a story in parallel.
- `[P]` models/catalogs/schemas within a story in parallel.
- Across stories: once Foundational is done, US1/US2 in parallel; US3/US4/US5/US6 in parallel with them (no shared notification dep); US7 after US2; US8 after US2.

---

## Implementation Strategy

### MVP first
Setup → Foundational → **US1 (crisis safety)** → STOP & validate (Scenario 1, incl. safety-net + false-positive). This is the highest-stakes, demoable slice.

### Incremental delivery
Add US2 (family loop + alerts) → US3 (med re-reminders) → US4 (memory) → US5 (evening) → US6 (wellbeing) → US7 (anti-scam/opt-out) → US8 (callback/Spanish/report). Each validated via its quickstart scenario before the next.

### Parallel team strategy
After Foundational: Dev A → US1; Dev B → US2 (+ enables US7/US8); Dev C → US3/US4/US5/US6 (notification-independent).

## Notes
- Commercial layer (subscription/FAQ/payment) is **out of scope** (spec → deferred to spec 003).
- Migrations are sequential `0017`–`0025`; keep `down_revision` chained and additive/backward-compatible.
- Verify each RED test fails before implementing; commit per task or logical group.
- T087 (US7, FR-041), T088 (US2, FR-013), T089 (US2, SC-004) are post-analysis remediation additions; execute them within their phase regardless of ID ordering. Total tasks: 89.

## Implementation status (`/speckit-implement`)

**US1 COMPLETE & green** (Setup → Foundational → US1 crisis detection, both layers): T001–T021, T089.
- apps/api: 1354 passed / 1 skipped, `mypy` clean (114 files), `ruff` clean. services/agent: 245 passed, `mypy` clean (17 files), `ruff` clean.
- Migrations 0017 (notifications substrate) + 0018 (follow_up_flags crisis cols) apply on `alembic upgrade head`.
- US1 ships BOTH crisis paths (FR-002): (a) the **deterministic safety net** — `crisis_watcher.py` → worker → `POST /v1/tools/raise_crisis`; (b) the **LLM-callable `raise_crisis`** — added as the 8th catalog tool, default-enabled, with `noop_raise_crisis` for sandbox mode, crisis prompt guidance in both default check-in prompts, and the watcher/worker enum-typed end to end. Both upsert the same urgent flag idempotently (source merges to `both`) and enqueue a PHI-minimized family alert.
- 2026-06-14 update: T019/T021 completed — `raise_crisis` is now the 8th tool in `TOOL_CATALOG`/`TOOL_NAMES`, registered in `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY`, default-enabled in both `agent_config` copies; all catalog-count/default-enabled pinned tests updated (7→8). Safety-net escalation remains guaranteed independent of operator tool config (armed unconditionally in the worker).

**US2 COMPLETE** ✅ (family relay & alerting):
- ✅ Data layer (T025/T026): migration `0019_family_contacts_tasks.py` (`family_contacts` + `family_tasks`, incl. `inbound_message_id` unique idempotency key), `FamilyContact`/`FamilyTask` models, `repositories/family_contacts.py` (CRUD + phone lookup + `list_alert_recipients` honoring `alert_prefs`) + `repositories/family_tasks.py` (open/delivered/closed state machine + idempotent `create_inbound_task`). Tests: `test_family_repos.py` (3).
- ✅ Inbound webhook (T022/T028/T029): `telnyx_inbound.py` (real Telnyx Ed25519 verify over `{ts}|{body}`, sig-first-then-replay, oracle-guard 401, `is_medically_unsafe` FR-015 screen), `schemas/inbound_sms.py`, `POST /webhooks/telnyx` route + family-task intake (FR-008 create per linked elder; FR-014 unmatched-sender safe default; FR-015 needs_safety_review; idempotent per Telnyx msg id). Tests: `test_telnyx_inbound.py` (6).
- ✅ Builtin (T030): `open_family_tasks` builtin — added to `variable_catalog.py` (phi=True), both substitutor mirrors (`prompt_vars.py`, `prompt_substitution.py`), and `resolve_builtin_vars` (new `open_family_tasks` param; callers in `routers/calls.py` ×2 + `livekit_dispatch.py` query `list_open_family_tasks` and pass messages). All builtin-catalog/parity pins bumped 11→12. Renders open task messages joined by "; ".
- ✅ Alerts + operator fallback (T033/T034/T088): `notifications.dispatch_family_alert` resolves family recipients via `family_contacts.alert_prefs` (keys `crisis`/`missed_call`, fail-open) and reports `had_contacts`. Crisis (`routers/tools.py`) now alerts every opted-in contact and, with NO contact, annotates the urgent flag for operators (replacing the `elder.meta["family_phone"]` interim). Missed-call alert fires at finalization (`calls.py schedule_retry` `delay is None`, dedupe `missed:{call_id}:{phone}`); with no contact it creates an idempotent routine `operator_alert` follow_up_flag. Tests: `test_family_alerts.py` (6), `test_notifications_fallback.py` (4), reworked `test_crisis_escalation.py` (7).
- ✅ Close tool + loop (T031/T023): `close_family_task` is the **9th** catalog tool (`TOOL_CATALOG`/`TOOL_NAMES`, `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY`, default-enabled in both `agent_config` copies, `noop_close_family_task` for sandbox; `api_client.close_family_task`). `POST /v1/tools/close_family_task` does `open → delivered` (contract); `task_id` is **optional** — omitted → `family_tasks.mark_all_delivered` closes every open non-safety-review task for the call's elder (the LLM sees only task TEXT), with an elder-scope guard (cross-elder → 404). All catalog-count/default-enabled pins bumped 8→9 (incl. `test_runtime.py`). Tests: `test_family_tasks.py` (4: intake→convey→close→no-repeat, explicit id, cross-elder 404, empty no-op) + agent `test_check_in.py` (4 new).
- ✅ Operator admin plane (T032/T027): `routers/admin_family.py` (registered in `main.py`) — family-contacts CRUD (`POST`/`GET`/`PATCH`/`DELETE /v1/admin/family-contacts`) + family-tasks `GET` (needs-review-first ordering) and `PATCH` operator transitions: `status:"open"` = approve a held `needs_safety_review` task (new `family_tasks.approve_family_task`), `status:"closed"` = close. `require_admin_session` for reads, `require_admin_role(ADMIN)` for mutations; every mutation audit-logged with NO PHI (UUIDs/field-names only). `schemas/family.py` (closes T027). Tests: `test_admin_family.py` (10: CRUD, unknown-elder 404, bad-phone 422, viewer 403, needs-review-first, approve/close, unknown 404, no-PHI audit).
- Final US2 snapshot: apps/api **1390 passed / 1 skipped**, `mypy` clean (120 files), `ruff` clean; services/agent **249 passed**, mypy + ruff clean. Migrations apply to head `0019`.

**US3 COMPLETE** ✅ (medication adherence with re-reminders):
- ✅ Data layer (T037/T038): migration `0020_medication_reminders.py` (`medication_reminders` + partial-unique `uq_medication_reminders_pending` = one `pending` per `(elder_id, medication_name)`), `MedicationReminder` model, `repositories/medication_reminders.py` (guarded state machine: `open_or_refresh` opens/increments/caps, `clear_pending`, `list_pending`; `MAX_REASK_ATTEMPTS=3`).
- ✅ Tool extension (T039): `POST /v1/tools/log_medication` unchanged shape, but `taken=false` opens/refreshes a pending re-ask and `taken=true` clears it; reaching the cap → routine `medication` `follow_up_flags` row (+`FOLLOWUP_FLAGS_TOTAL`) so Clara stops nagging. reason names the med (clinical, BAA-DB only; flag.created webhook omits it).
- ✅ Builtin (T040): `pending_med_reasks` builtin — `variable_catalog.py` (phi=True), both substitutor mirrors (`prompt_vars.py`, `prompt_substitution.py`), and `resolve_builtin_vars` (new `pending_med_reasks` param; callers in `routers/calls.py` ×2 + `livekit_dispatch.py` query `list_pending` and pass med names). All builtin-catalog/parity pins bumped 12→13. Comma-joins pending med names.
- ✅ Prompt guidance (T041): step-2 medication re-ask guidance referencing `{{pending_med_reasks}}` added to both default prompts (`checkin_flow_instructions` + `inbound_personalization_template`) in BOTH agent_config mirrors; "re-ask only those, once, never nag." `test_check_in.py` instruction pins updated for the new substituted token.
- ✅ Tests: `test_medication_reminders.py` (8: open, clear, refresh-dedupe, taken-noop, distinct-med independence, cross-call clearing, reopen-after-cap new-cycle, partial-unique IntegrityError) + `test_medication_reminders_flow.py` (3: re-ask carried next touch → cap → routine flag → no nag; confirm-before-cap clears; inbound-call carry) + `test_builtin_vars.py` (2 new) + `test_prompt_substitution_parity.py` (cross-mirror agent_config prompt-body parity).
- ✅ Post-implementation adversarial review (4 lenses: state-machine, PHI, parity, tests; 13 agents): 0 CRITICAL/HIGH, 9 findings → 6 confirmed (all LOW/MED) and fixed — cap-timing comment + data-model wording corrected; inbound-carry, cross-call-clear, reopen-after-cap, partial-unique constraint, and cross-mirror prompt parity now tested. 3 dismissed (reopen-after-cap is intended per-cycle FR-019; `next_reminder_at` reserved; py3.14 except-style).
- Final US3 snapshot: apps/api **1405 passed / 1 skipped**, `mypy` clean (121 files), `ruff` clean; services/agent **249 passed**, mypy + ruff clean. Migrations apply to head `0020`.

**US4 COMPLETE** ✅ (personalized memory across calls):
- ✅ Data layer (T045/T046): migration `0021_personal_facts_summaries.py` (`personal_facts` + `conversation_summaries`, unique `call_id` for idempotent summarization), `PersonalFact`/`ConversationSummary` models, `repositories/personal_facts.py` (`create`, `list_active`, `list_active_keys`) + `repositories/conversation_summaries.py` (`create` on_conflict_do_nothing, `get_latest`, `get_for_call`). `schemas/personalization.py` (T047, closed `FactCategory`).
- ✅ record_personal_fact tool (T048): `POST /v1/tools/record_personal_fact` (always `source='elder_stated'`, phi default true) + agent `@function_tool` (with `date` for important_dates) + sandbox noop + `api_client` + both `check_in` registries + `tool_catalog` (9→10) + **default-enabled in both `ToolsConfig` mirrors**.
- ✅ Summarization (T049/T050): `summarization.py` — one Vertex turn (ADC `vertexai=True`, never the Dev API; transcript PHI stays on BAA) → `conversation_summaries` recap + extracted `personal_facts` (dedup vs FULL active set); idempotent per call, flag-gated (`summarization_enabled`, ship-inert), defensive JSON parse (fence-strip, malformed→raw), bounds (12k transcript / 500 fact / 20 facts). Triggered from BOTH `end_call` and the `room_finished` webhook (winner-only completion transition ⇒ one task; unique `call_id` = defense-in-depth).
- ✅ Memory builtins (T051): `personal_facts`, `last_call_summary`, `open_plans`, `important_dates` (all phi=True) via `build_memory_params` (±1-day anniversary window, Feb-29 observed on Feb-28 in non-leap years, year-boundary wrap); carried at all 3 sites (outbound, inbound, retry); mirrored in `variable_catalog`/`prompt_substitution`/agent `prompt_vars` (pins 13→17); woven into both default prompts (step renumber 3→4).
- ✅ Tests: `test_tools_personal_fact.py` (5), `test_summarization.py` (11: writes/extracts, idempotent, no-transcript, malformed-JSON, disabled-gate, dedup-across-calls, off-enum+cap, fenced-JSON, category-consistency), `test_memory_carryforward.py` (5), `test_builtin_vars.py` (+6: memory render/empty, build_memory_params windows/empty/year-boundary/leap-day), agent `test_api_client_tools.py` (+2) + `test_check_in.py` (+2 non-circular default-exposes + structured-forwarding); all catalog/parity pins bumped (12/9→17/10).
- ✅ Post-implementation adversarial review (4 lenses: state-machine, PHI, parity, tests; 22 agents): **0 CRITICAL, 1 HIGH, rest MED/LOW**. Fixed: HIGH record_personal_fact missing from default enabled (memory-write was dead by default); MED agent tool couldn't pass a date (elder-stated important_dates never surfaced); MED extracted-fact dedup read the 50-row cap (duplicate growth >50 facts); LOW Feb-29 + stale "8 tools" comment. Dismissed: the "double Vertex call" race (completion transition is winner-only ⇒ one task enqueued; `get_for_call`+unique `call_id` are defense-in-depth). Accepted low-risk gaps: agent-side `FactCategory` parity test + retry-site memory carry test (fail-closed; pattern tested at 2/3 sites).
- Final US4 snapshot: apps/api **1434 passed / 1 skipped**, `mypy` clean (125 files), `ruff` clean; services/agent **253 passed**, mypy + ruff clean. Migrations apply to head `0021`.

---

**US8 COMPLETE** ✅ (callback auto-dial, Spanish callback, monthly family report) — 2026-06-15

- ✅ Callback auto-dial (T073/T074/T075): migration `0024_callback_autodial.py` widens the `ck_callback_requests_status` CHECK with `scheduled`/`dialed` + adds `dispatched_call_id` (SET NULL) and `profile_override` (SET NULL) + `idx_callback_requests_due`. `repositories/callback_requests.py`: `create_callback_request` gains `profile_override`; new `list_due_open_ids` / `claim_open_for_dial` (FOR UPDATE) / `mark_scheduled` / `reconcile_dialed`. `callback_dialer.py` poller: gather due `open` ids → per-row txn (advisory phone lock → DNC → `quiet_hours.next_allowed(max(requested_at, now))` clamp → `create_materialized_root` idempotency `callback:{id}`, SAVEPOINT verified-replay) → `open→scheduled`; reconcile `scheduled→dialed` once the Call leaves QUEUED. Wired behind `callback_dialer_poller_enabled`. NOT applied to the daily-cap (an elder-requested callback is not autonomous). **No daily-cap, honors DNC + quiet-hours + idempotency (FR-030/031).**
- ✅ Spanish callback (T076): `set_spanish_callback` is the **15th** catalog tool. Endpoint sets `elders.meta["language"]="es"` (immutable reassign so JSONB dirties) + creates a callback flagged Spanish (`requested_at=now`, `profile_override=SPANISH_PROFILE_ID` when configured). Mirrored api↔agent: catalog (14→15), both `ToolsConfig` enabled defaults, `api_client`, `check_in` live `@function_tool` + `noop_*` + `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY`; SPANISH prompt guidance woven byte-identically into both default prompt blocks (FR-040/SC-011).
- ✅ Monthly family report (T077/T078): migration `0025_family_reports.py` (`family_reports`, unique `(elder_id, period_month)`, status CHECK `sent`/`no_contact`). `FamilyReport` model + `repositories/family_reports.py` (`create` ON CONFLICT DO NOTHING, `list_reports`, `get_report`, `get_for_month`). `family_report_job.py` monthly poller: prior-month anchor (elder-local), per-elder aggregate (calls_completed/avg_mood/med_adherence/survey → `metrics` PHI), Vertex narrative (`vertexai=True`+ADC) with deterministic fallback, **fixed PHI-free family SMS** (`notifications.build_family_report_body`), no-contact → `no_contact` status (operator follow-up, FR-013); skips elders with 0 calls (SC-012). Wired behind `family_report_poller_enabled`. **Constitution II: trends/narrative stay in Postgres; SMS carries no clinical content (T083).**
- ✅ Admin reads (T079): `routers/admin_tools.py` widens callback-requests status filter (+`scheduled`/`dialed`) + `CallbackRequestSummary.dispatched_call_id`; `routers/admin_family.py` adds `GET /v1/admin/family-reports` (list) + `POST /family-reports/{id}/resend` (ADMIN, re-enqueue PHI-free SMS, 409 on no-contact, PHI-free audit) + `FamilyReportOut`.
- ✅ Tests: `test_callback_dialer.py` (8: materialize/quiet-hours-defer/DNC/idempotent/future-not-due/null-time/reconcile/profile-override-propagation), `test_tools_spanish_callback.py` (4: meta+callback/configured-profile/403/409), `test_family_report.py` (4: generate+PHI-free SMS/idempotent/no-contact/no-calls); all catalog+enabled+registry pins bumped 14→15; FK-cascade test made source-column-aware (callback_requests now has two `calls` FKs); pinned admin field-set + migration-cascade tests updated.
- ✅ Post-implementation adversarial review (4 lenses: correctness/concurrency, PHI/security, parity/contract, spec/edge; 34 agents, 3-skeptic verify): **0 CRITICAL, 0 genuine HIGH defects.** Confirmed PASS: authorization (403/409), PHI-free SMS + audit + poller logs, Vertex-not-Gemini, 15-tool lockstep, single-head migration chain. Acted on review: added `model_version` to `FamilyReportOut` (operator provenance) + added the profile_override-propagation test (closed the SC-011 end-to-end gap). Documented (deferred, no US8 task): the "evening + callback collision" edge case — cross-schedule per-elder dedup belongs in the shared dispatch layer, noted as a known limitation in `callback_dialer.py`. The recurring `except`-paren display artifact was pre-empted by the review prompt's ground-truth rule (0 false syntax findings). No review-agent repo mutation (verified via `git status`).
- Final US8 snapshot: apps/api **1510 passed / 1 skipped**, `mypy` clean (132 files), `ruff` + format clean; services/agent **253 passed**, mypy + ruff clean. Single alembic head `0025` (linear 0022→0023→0024→0025). Principle I held (no `usan_api` import in the agent).

---

## Phase 11 (Polish) COMPLETE ✅ — engineering done; 2 operator-gated tasks remain — 2026-06-15

- ✅ **T080** api↔agent tool/builtin parity: new `apps/api/tests/test_api_agent_tool_parity.py` cross-checks (AST, no import edge) the agent `_TOOL_REGISTRY`/`_TEST_TOOL_REGISTRY` keys + values, every `@function_tool` callable, and the default `enabled` list against the API `TOOL_NAMES`; builtins/prompt-bodies covered by `test_prompt_substitution_parity.py`; agent side self-pins. 4 tests; drift-detection proven.
- ✅ **T081** gates: apps/api **95%** coverage (1524 passed / 1 skipped), services/agent **88%** (253 passed); `ruff` + `ruff format` + `uv run mypy` clean in both units.
- ✅ **T082** no-PHI in logs/audit/webhooks: pre-existing comprehensive coverage (`test_webhook_events.py::test_phi_exclusions_pinned`; PHI-free admin audit details; type-only poller logs) + NEW `test_admin_family` resend-audit coverage (`detail == {"recipients": N}`).
- ✅ **T083** compliance (engineering): no-clinical-content family-SMS tests + ship-disabled fallback verified (`TELNYX_MESSAGING_ENABLED` master gate; outbox backlogs when off). ⛔ Human Telnyx-Messaging-BAA sign-off remains an open checkbox in `docs/compliance/clara-care-parity-go-live.md`.
- ✅ **T084** docs + env flags: the 8 new flags plumbed through `docker-compose.yml` (verified inert in the merged prod render via `docker compose config`) + documented in both `.env*.example`; locked by `test_infra_clara_parity_env.py`; compliance doc updated; profile prompts already shipped in `DEFAULT_AGENT_CONFIG`.
- ⚠️ **T085** quickstart 1–8 end-to-end: logic covered by the automated suite; true live run needs `make up` + real telephony/Vertex/Cartesia (operator-gated).
- ⚠️ **T086** VM `.env` keys before the deploy tag: compose plumbing verified ready; the IAP-SSH `.env` refresh is a production, deploy-time operator action (not done autonomously).
- ✅ Post-Polish adversarial review (3 lenses: test-validity / PHI-compliance / deploy-correctness): **0 CRITICAL, 0 unaddressed HIGH.** Acted on review: fixed the compliance doc "four flags" inaccuracy, added a 4th parity test (`@function_tool`↔registry, which caught a real bug in its first draft → the `noop_*` stubs are also decorated), added `adherence`/`survey` to the SMS clinical-term scan, added a prod-overlay comment explaining the no-re-pin rationale. `resend` no-dedupe is intentional pre-existing US8 design (re-send semantics; PHI-free). No review-agent repo mutation (`git status` verified).

**Feature status: US1–US8 complete; Polish engineering complete. Remaining before go-live (operator/deploy actions, NOT code): T083 BAA sign-off, T085 live end-to-end, T086 VM `.env` refresh before the `v*` tag.**
