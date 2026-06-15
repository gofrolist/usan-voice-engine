# Phase 0 Research: Clara Care Parity

All decisions below are grounded in a line-cited read of the current code. Each resolves a "how do we build this on what exists" question raised by the spec.

---

## Decision 1 — Evening call window via a `slot` discriminator

**Decision**: Add a `slot` column to `call_schedules` (values `morning` | `evening`) and change the unique constraint from `UNIQUE(elder_id)` to `UNIQUE(elder_id, slot)`. Backfill existing rows as `slot='morning'`. Each slot row keeps its own `enabled`, window, and `days_of_week`, so the evening call toggles independently.

**Rationale**: `call_schedules` today enforces one schedule per elder via `UNIQUE elder_id` (`db/models.py:449-454`); `get_by_elder()` uses `scalar_one_or_none()` (`repositories/call_schedules.py:57-59`) and `_materialize_one_schedule()` reads a single window with no iteration (`schedule_orchestrator.py:298-342`). A discriminator is the smallest change: the materialization key pattern `sched:` (`schedule_orchestrator.py:394`) becomes per-slot, the poller iterates the (now ≤2) rows per elder, and `get_by_elder` returns a list.

**Alternatives considered**: (a) second `*_evening` column pair on the same row — rejected, duplicates the window/days math and complicates poller branching; (b) child `call_schedule_slots` table — rejected as over-normalized for a 2-row-max cardinality and forces dual-claim logic (YAGNI, Principle VII).

---

## Decision 2 — Callback auto-dial as a new poller phase reusing the dial pipeline

**Decision**: Add a `callback_dialer` poller phase that claims `callback_requests` rows that are `open` with `requested_at <= now`, clamps the time via `quiet_hours.next_allowed(requested_at, elder.timezone)`, checks DNC, and creates a QUEUED `Call` via `calls_repo.create_call()` with `idempotency_key = "callback:{id}"` and `scheduled_at` = the clamped time. The existing retry poller then dials it. Transition the request `open → scheduled` (storing `dispatched_call_id`), and `→ dialed` once the call starts.

**Rationale**: Callbacks are logged but never dialed today — `schedule_callback` creates a `CallbackRequest` + `callback.created` webhook but no `Call` (`routers/tools.py:143-167`). The dial machinery is fully reusable: daily schedules already work by *materializing a QUEUED Call with `scheduled_at`* that the retry poller claims and dials (`schedule_orchestrator.py:23-25`, `retry_orchestrator.py:86-107`, `livekit_dispatch.dispatch_and_dial`). `quiet_hours.next_allowed()` already clamps to the elder-local legal window (`quiet_hours.py:25-62`) and is the same gate used by retry scheduling (`calls.py:292-297`). `CallbackRequest` already has `requested_at` (`db/models.py:400`).

**Alternatives considered**: dialing inline at request time — rejected because the requested time is in the future and must survive process restarts and honor quiet-hours; the poller+QUEUED-Call pattern already guarantees both.

---

## Decision 3 — Inbound SMS via a new HMAC-verified Telnyx webhook

**Decision**: Add `POST /v1/webhooks/telnyx` as the first inbound-message handler, in `routers/webhooks.py` alongside the LiveKit handler. Verify the Telnyx signature (Ed25519/HMAC per Telnyx's `Telnyx-Signature-Ed25519` + `Telnyx-Timestamp`) in a new `telnyx_inbound.py` verifier mirroring the structure of `webhook_signing`/`livekit_webhooks`. Route a verified `message.received` to either family-task intake (Decision 4) or opt-out (Decision 8).

**Rationale**: No inbound SMS handler exists today — `routers/webhooks.py` handles only LiveKit (`/webhooks/livekit`, verified via `verify_livekit_webhook`, lines 31-41); a grep for `message.received`/`STOP`/`inbound` returns nothing. The verification precedent (`webhook_signing.py:29-51`, `livekit_webhooks.py:12-34` with timestamp replay protection) is the template. Telnyx is already the SMS/voice provider.

**Alternatives considered**: polling Telnyx for inbound messages — rejected (webhook is the supported, low-latency path and matches the existing LiveKit pattern).

---

## Decision 4 — Family contacts + tasks as new tables; tasks injected as a builtin var

**Decision**: New `family_contacts` (per elder; phone, relationship, alert prefs) and `family_tasks` (source contact, elder, message, status `open → delivered/closed`, `needs_safety_review` flag). Inbound SMS from a number matching a `family_contacts.phone_e164` creates an `open` family_task. `resolve_builtin_vars` adds an `open_family_tasks` builtin; a new `convey_family_task`/`close_family_task` tool marks delivery. Tasks that conflict with medical safety are flagged `needs_safety_review` and not injected.

**Rationale**: No family concept exists in the model (`db/models.py` full read). The builtin-var injection path is the established way to give the agent per-call context (`builtin_vars.resolve_builtin_vars:70-109` → dispatch metadata → `prompt_vars.build_vars` → `{{token}}` substitution); adding `open_family_tasks` mirrors `today_meds`/`last_check_in`. Closing on call end mirrors the existing tool→`/v1/tools/*` write pattern.

**Alternatives considered**: storing family contacts in `elders.meta` JSONB — rejected because they need their own identity (phone lookup on inbound, per-contact alert prefs, many-per-elder); a typed table matches conventions.

---

## Decision 5 — Crisis: deterministic STT matcher + LLM, one `raise_crisis` tool, resources as code

**Decision**: Add `crisis_watcher.py` agent-side, a phrase matcher subscribed to `session.on("user_input_transcribed")` exactly like `VoicemailWatcher` (`voicemail.py:44-72`, subscribed at `worker.py:345`). On a match it calls the new `raise_crisis(category)` tool *and* signals the agent to deliver the resource (via `session.say`/interruption). The LLM may also call `raise_crisis` from its own judgment. The tool endpoint records an escalation (extending `follow_up_flags` — Decision 6), returns the correct resource script from a code catalog `emergency_resources.py` (988 / 911 / Adult Protective Services / Poison Control 1-800-222-1222), and triggers a family alert (Decision 7).

**Rationale**: The spec requires LLM + a deterministic safety net (FR-002). The agent has a proven, language-independent, LLM-independent STT phrase-matching primitive already in production for voicemail; reusing it gives a sub-second deterministic trigger. Catalog-as-code matches the voice/model catalog pattern and satisfies FR-006 (resources are data, not prompt-baked). Both detection paths converging on one tool keeps the escalation/alert logic in one place.

**Alternatives considered**: LLM-only (rejected by the spec decision); a separate classifier model (rejected — adds cost/latency and another PHI surface; deterministic phrases + LLM cover the stated categories).

**Open item**: the benign **control set** for SC-002 (≤2% false escalation) must be authored as test data; phrase lists are tuned against it (tracked in tasks).

---

## Decision 6 — Crisis escalation extends `follow_up_flags`

**Decision**: Add columns to `follow_up_flags`: `crisis_category` (text), `detection_source` (`llm` | `safety_net` | `both`), `resource_offered` (text key), `family_notified` (bool). A crisis is a `severity='urgent'` flag with these populated. No new table.

**Rationale**: `follow_up_flags` already models urgent issues with a status machine (`open/acknowledged/resolved`), severity, category, and deliberately outlives elder deletion ("clinical context must outlive an elder row removal", `db/models.py:366-386`); status/severity are `Text + CHECK` (migration 0013), so widening is additive without an ORM enum recompile. A crisis is one-to-one with a flag, so extension beats a parallel table (Principle VII). The existing `flag.created` webhook keeps its PHI-safe payload (no `category`/`reason`/`elder_id` pairing).

**Alternatives considered**: standalone `crisis_escalations` table — rejected (1:1 with a flag; duplicates the status machine).

---

## Decision 7 — Family notifications: nullable-call SMS + a flush poller, PHI-minimized

**Decision**: Make `sms_messages.call_id` nullable and add a `kind` column (`in_call` | `family_alert` | `family_report` | `opt_out_ack`). Family alerts/reports are `sms_messages` rows with `call_id = NULL`, `elder_id` set, built by a new `notifications.py` from PHI-minimized templates, and delivered by a new `notification_outbox` poller phase that claims `call_id IS NULL` pending rows and calls `telnyx_messaging.send_sms`. Dedupe on a natural key (`crisis:{flag_id}`, `missed:{call_id}`).

**Rationale**: `telnyx_messaging.send_sms(settings, to_number, body)` is already a general-purpose sender (`telnyx_messaging.py:20-52`), but `SmsMessage` is call-scoped (`call_id`/`elder_id` NOT NULL, `db/models.py:410-437`) and flushing is tied to `flush_pending_sms(call_id)` (`sms_outbox.py:39-97`). Relaxing `call_id` + a sibling flush poller reuses the table, the sender, and the at-least-once per-row-commit pattern with minimal change. PHI-minimization keeps Principle II intact (alerts say "please check in with your family member," no clinical detail), consistent with `sms_render` already dropping PHI builtins.

**Alternatives considered**: a parallel `notifications` table — rejected (duplicates SMS storage/delivery); the outbound webhook outbox — rejected (it is event-subscription to *operator* endpoints, not a contact-SMS channel; `webhook_events.py:35-41`).

---

## Decision 8 — Opt-out: spoken tool + inbound STOP, both write DNC

**Decision**: A `register_opt_out` agent tool (spoken "stop calling") and the inbound `STOP` keyword path both call the existing DNC enrollment (`dnc_repo` add + serialize via `lock_phone`) and enqueue a `family_alert`/operator notification of the opt-out. DNC is already enforced on every outbound dial path.

**Rationale**: DNC add/remove + serialization already exist (`routers/dnc.py:13-28`, `dnc_repo.lock_phone`/`is_blocked` used at `routers/calls.py:133-158`); enforcement on outbound is automatic. The only gaps are the two *capture* paths (voice + inbound SMS), which reuse the existing enrollment. Notifying family/operator satisfies FR-039.

---

## Decision 9 — Post-call memory is API-side Vertex; PHI stays on BAA infra

**Decision**: Personal-fact extraction and conversation summarization run **API-side** after the call, triggered off the existing transcript flush / `call.completed`, using **Vertex AI via ADC** (reuse the admin text-test Vertex path). Write `conversation_summaries` (per call) and `personal_facts` (per elder, categorized). Carry forward via new builtins (`last_call_summary`, `open_plans`, `personal_facts`, `important_dates`) in `resolve_builtin_vars`. An in-call `record_personal_fact` tool captures explicit facts immediately.

**Rationale**: The agent has **no LLM available at session shutdown** — STT/LLM live only during the call and are torn down before shutdown callbacks run (research §4; `transcript.py:56-64` flushes raw history only). Summarization therefore must be API-side, where a Vertex client already exists for the admin text-test. Vertex-via-ADC is the constitution's required LLM path (Principle II); the Gemini Developer API is **not** BAA-covered (memory: gemini-dev-api-not-baa-covered). Builtin-var carryforward is the established context channel.

**Open confirm (mitigation flag in Constitution Check)**: Confirm **Telnyx messaging is BAA-covered** before any PHI-adjacent family SMS goes live; if not, keep family SMS strictly PHI-minimized (no clinical content) or gate behind a BAA confirmation. Tracked as a task + a compliance checklist item.

---

## Decision 10 — Surveys, activities, re-reminders, Spanish: minimal new tables + catalog-as-code

**Decision**:
- **Survey**: `wellbeing_survey_results` table (loneliness/mood/satisfaction + period); a `survey_due` builtin computed from "no result this calendar month"; a `record_survey` tool. Once-per-month enforced by a unique `(elder_id, period_month)`.
- **Activities**: `activities_catalog.py` code constant (breathing/memory/game entries) + `activity_history` table (per elder, recent use); a `get_activity` tool returns the least-recently-used catalog entry not used recently and records the use (least-recently-used reuse when exhausted).
- **Medication re-reminders**: `medication_reminders` table (`elder_id`, `medication_name`, `attempt_count`, `status` pending/cleared/capped, `next_reminder_at`); the not-taken branch opens one; a `pending_med_reasks` builtin surfaces them next touch; confirmation clears; cap → routine `follow_up_flags` row.
- **Spanish**: `set_spanish_callback` tool stores `elders.meta["language"]="es"` and creates a `CallbackRequest` with a `profile_override` pointing at a Spanish-configured profile (Spanish `stt.language`/`voice.language`); the Decision-2 auto-dial path dials it. No mid-call language switch (LiveKit 1.x STT is fixed per session; research §5).

**Rationale**: `WellnessLog`/`MedicationLog` are minimal adherence logs not shaped for survey/reminder state (research §4/§5), so dedicated tables are cleaner; activities mirror catalog-as-code; Spanish reuses the callback machinery + per-call profile config (`AgentConfig.stt.language`/`voice.language`, `pipeline.py:63-94`) rather than unsupported mid-call reconfiguration. Language stored in `elders.meta` matches the existing `medication_schedule` convention.

**Alternatives considered**: surveys/reminders inside `wellness_logs.raw` JSONB — rejected (need queryable once-per-month + reminder state machines); activities in a DB table — rejected (static curated content fits catalog-as-code, Principle VII).

---

## Summary of grounded extension points

| Need | Reused primitive (cited) | New code |
|------|--------------------------|----------|
| Evening call | `call_schedules` + slot, `schedule_orchestrator` per-slot | migration + poller iterate |
| Callback auto-dial | `quiet_hours.next_allowed`, `calls_repo.create_call`, retry poller | `callback_dialer` phase |
| Inbound SMS | `routers/webhooks.py` + signing pattern | `telnyx_inbound.py`, `/webhooks/telnyx` |
| Family alerts | `telnyx_messaging.send_sms`, `sms_messages` (call_id nullable) | `notifications.py`, `notification_outbox` |
| Crisis safety net | `VoicemailWatcher` on `user_input_transcribed` | `crisis_watcher.py`, `raise_crisis` tool, `emergency_resources.py` |
| Crisis escalation | `follow_up_flags` (+cols) | crisis columns |
| Memory | `resolve_builtin_vars` → dispatch → `prompt_vars`; Vertex text-test path | `summarization.py`, new builtins, `record_personal_fact` |
| Surveys/activities/re-reminders | tool/registry pattern, catalog-as-code | new tables + tools |
| Opt-out | `dnc_repo` enroll + enforce | capture paths |
