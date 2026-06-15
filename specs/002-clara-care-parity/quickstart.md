# Quickstart: Validating Clara Care Parity

Validation scenarios that prove each user story end-to-end. Prerequisites and commands reuse the existing dev stack; details of schemas/endpoints live in [contracts/](./contracts/) and [data-model.md](./data-model.md).

## Prerequisites

```bash
# API
cd apps/api && uv sync && uv run pytest -v
ruff check . && ruff format . && uv run mypy

# Agent
cd services/agent && uv sync && uv run pytest -v
ruff check . && ruff format . && uv run mypy

# Stack (local)
make up          # builds usan-agent-base:local if missing, then compose up
make logs
```

Set local env for the new behaviors: `TELNYX_MESSAGING_ENABLED=true`, Telnyx messaging creds, `TELNYX_INBOUND_PUBLIC_KEY`, and the new poller flags (callback dialer, notification outbox, family report). Vertex ADC must be available for summarization (same as the admin text-test).

## Scenario 1 — Crisis safety net (US1, P1)

1. Run an agent test session (or a scripted call) where the user utterance contains an explicit crisis phrase for each category.
2. Verify, per category, that: the correct resource is spoken (988/911/APS/Poison Control), a `follow_up_flags` row is created with `severity=urgent` + crisis columns, and a `family_alert` SMS is enqueued.
3. **Safety-net proof**: feed a crisis phrase while the LLM is stubbed to NOT call `raise_crisis`; confirm the deterministic `crisis_watcher` still triggers the escalation + resource.
4. **False-positive proof**: run the benign control set; assert escalations ≤ 2% (SC-002).

Validates FR-001..FR-006, SC-001, SC-002.

## Scenario 2 — Family task loop & alerts (US2)

1. `POST /v1/admin/family-contacts` for an elder.
2. POST a signed `message.received` to `/v1/webhooks/telnyx` from that contact ("remind mom to drink water"); confirm an `open` `family_tasks` row.
3. Run the elder's next call; confirm the task is conveyed (it appears via the `open_family_tasks` builtin) and `close_family_task` moves it to `delivered`; a following call does not repeat it.
4. Force a missed call (exhaust retries) and an urgent flag; confirm a `missed`/`crisis` family SMS is enqueued and deduped.
5. Send a task from an unregistered number; confirm no task is created.

Validates FR-007..FR-015, SC-003, SC-004.

## Scenario 3 — Medication re-reminders (US3)

1. In a call, log a medication as `taken=false` via `log_medication`; confirm a `medication_reminders` row (`pending`).
2. Next touch: confirm `pending_med_reasks` surfaces it and the agent re-asks.
3. Confirm taken; confirm the reminder is `cleared`.
4. Drive re-asks to the cap; confirm `capped` + a routine `follow_up_flags` row, and no further nagging.

Validates FR-016..FR-019, SC-006.

## Scenario 4 — Cross-call memory (US4)

1. Seed `personal_facts` (family, routine, important date) for an elder.
2. Run call 1; have the elder state a new fact and a plan; confirm post-call summarization writes a `conversation_summaries` row and extracts the fact (Vertex).
3. Run call 2; confirm the prompt carries `last_call_summary`, `open_plans`, `personal_facts`, and (if near) `important_dates`, and the agent references at least one.

Validates FR-020..FR-026, SC-007, SC-013 (verify no PHI left Vertex/Postgres).

## Scenario 5 — Evening calls (US5)

1. Create morning + evening schedules (`slot`) for an elder; run the scheduler poller on an enabled day; confirm two QUEUED calls in the right windows.
2. Disable the evening slot; confirm only the morning call materializes.
3. Put the elder on DNC; confirm neither materializes.

Validates FR-027..FR-029, FR-031, SC-005.

## Scenario 6 — Survey & activities (US6)

1. Make an elder survey-due; run a call; confirm `record_survey` writes one `wellbeing_survey_results` for the month; a second call that month does not repeat it.
2. Run two low-mood calls; confirm `get_activity` returns different activities until the catalog is exhausted (`activity_history`).

Validates FR-032..FR-035, SC-008, SC-009.

## Scenario 7 — Anti-scam & opt-out (US7)

1. In a call, describe a scam; confirm Clara warns + explains red flags (prompt content).
2. Say "stop calling"; confirm `register_opt_out` adds DNC + notifies; confirm no future outbound.
3. POST a signed `STOP` to `/v1/webhooks/telnyx`; confirm DNC add + `opt_out_ack`.

Validates FR-036..FR-039, SC-010.

## Scenario 8 — Callback auto-dial, Spanish, family report (US8)

1. Request a callback for a near-future time; run the callback dialer; confirm a QUEUED `Call` (`idempotency_key=callback:{id}`) at the quiet-hours-clamped time, then dialed.
2. Request a callback inside quiet hours; confirm deferral to next allowed time.
3. Speak Spanish; confirm `set_spanish_callback` sets `meta["language"]="es"` + a callback with a Spanish profile override; confirm it dials with Spanish STT/TTS.
4. Run the monthly report job for an elder with a month of data; confirm a `family_reports` row + a PHI-minimized SMS to the family contact.

Validates FR-030, FR-040, FR-012, SC-011, SC-012.

## Cross-cutting checks

- `uv run mypy` + `ruff` clean in both units; ≥80% coverage.
- api↔agent parity contract test passes for every new tool and builtin.
- Inbound webhook rejects unsigned/expired payloads.
- No PHI in webhook payloads, audit detail, or family SMS bodies.
