# Phase 1 Data Model: Clara Care Parity

Conventions (from `apps/api/src/usan_api/db/models.py`): SQLAlchemy `Mapped[...]`/`mapped_column`, `Base` declarative; UUID PKs (`server_default text("gen_random_uuid()")`) for top-level entities, `BigInteger` autoincrement for high-volume log/child rows; `created_at`/`updated_at` as `DateTime(timezone=True)` with `func.now()`; JSONB with `server_default text("'{}'")`; status as `Text` + `CHECK` constraint (not PG enums) so values widen without ORM recompile; PG enums via `SAEnum(..., values_callable=_enum_values)` only for stable small sets. All timezone math in Python `zoneinfo`, never SQL. PHI governance is by convention (no per-column markers).

---

## New tables

### `family_contacts` (UUID PK)
A person linked to an elder who can send tasks and receive alerts/reports.

| Field | Type | Notes |
|------|------|------|
| `id` | UUID PK | |
| `elder_id` | UUID FK→elders, **no ondelete cascade** | contact context survives elder soft changes; many contacts per elder |
| `name` | Text | |
| `phone_e164` | Text, indexed | inbound SMS lookup key; not globally unique (a number may relate to >1 elder) |
| `relationship` | Text | e.g. "daughter", "neighbor" |
| `alert_prefs` | JSONB, default `{}` | which alert kinds this contact receives (missed/crisis/report) |
| `created_at` / `updated_at` | timestamptz | |

Index: `(phone_e164)` for inbound routing; `(elder_id)`.

### `family_tasks` (BigInteger PK)
A short instruction from a family contact to convey to the elder.

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `elder_id` | UUID FK→elders | target |
| `family_contact_id` | UUID FK→family_contacts, nullable | source (null if operator-entered) |
| `message` | Text | the instruction (e.g. "remind mom to drink water") |
| `status` | Text + CHECK `('open','delivered','closed','needs_review')`, default `'open'` | |
| `needs_safety_review` | Boolean, default false | set when task conflicts with medical safety |
| `delivered_call_id` | UUID FK→calls, nullable | which call conveyed it |
| `created_at` / `status_updated_at` / `status_updated_by` | | mirrors follow_up_flags audit fields |

**State machine**: `open → delivered → closed`; `open → needs_review` (operator) → `open`/`closed`. Only `open` (and not `needs_safety_review`) tasks are injected as `open_family_tasks`.

### `personal_facts` (BigInteger PK)
Durable categorized knowledge about an elder.

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `elder_id` | UUID FK→elders | |
| `category` | Text + CHECK `('person','routine','preference','important_date','health_context')` | |
| `content` | Text | natural-language fact |
| `structured` | JSONB, default `{}` | optional (e.g. date for `important_date`: `{"date":"2026-07-04","label":"birthday"}`) |
| `source` | Text + CHECK `('operator','elder_stated','extracted')` | |
| `active` | Boolean, default true | superseded facts set false (update-not-duplicate, edge case) |
| `phi` | Boolean, default true | health_context defaults PHI=true |
| `created_at` / `updated_at` | | |

Index: `(elder_id, active)`; `(elder_id, category)`.

### `conversation_summaries` (BigInteger PK)
Carry-forward of what was discussed / planned on a call.

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `call_id` | UUID FK→calls, ondelete CASCADE | one per completed call |
| `elder_id` | UUID FK→elders | |
| `summary` | Text | short recap (Vertex-generated, PHI on BAA infra) |
| `open_plans` | JSONB, default `[]` | stated intentions to follow up (e.g. ["doctor visit tomorrow"]) |
| `model_version` | Text | summarization model id (audit) |
| `created_at` | timestamptz | |

Index: `(elder_id, created_at desc)` for "most recent summary".

### `medication_reminders` (BigInteger PK)
Pending re-ask for a not-taken medication.

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `elder_id` | UUID FK→elders | |
| `medication_name` | Text | |
| `status` | Text + CHECK `('pending','cleared','capped')`, default `'pending'` | |
| `attempt_count` | SmallInteger, default 0 | not-taken reports recorded *after* the first open; cap at `MAX_REASK_ATTEMPTS` |
| `next_reminder_at` | timestamptz, nullable | reserved for a future per-reminder cooldown (not yet read; re-ask cadence is "next call") |
| `opened_call_id` / `cleared_call_id` | UUID FK→calls, nullable | |
| `created_at` / `updated_at` | | |

**State machine**: not-taken → `pending` (attempt_count=0); each repeated not-taken report increments; confirmation → `cleared`; reaching cap → `capped` + create routine `follow_up_flags` row. A fresh not-taken report after `capped` opens a NEW pending cycle (per-cycle cap, FR-019). Partial unique: one `pending` per `(elder_id, medication_name)`.

### `wellbeing_survey_results` (BigInteger PK)
Structured monthly survey outcome.

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `call_id` | UUID FK→calls, nullable | call it ran on |
| `elder_id` | UUID FK→elders | |
| `period_month` | Date | first-of-month anchor |
| `loneliness` | SmallInteger, nullable | scale |
| `mood` | SmallInteger, nullable | scale |
| `satisfaction` | SmallInteger, nullable | scale |
| `raw` | JSONB, default `{}` | extensibility |
| `created_at` | timestamptz | |

Unique: `(elder_id, period_month)` enforces once-per-month (FR-032 / SC-008).

### `activity_history` (BigInteger PK)
Per-elder record of mood-boosting activities used (catalog itself is code).

| Field | Type | Notes |
|------|------|------|
| `id` | BigInteger PK | |
| `elder_id` | UUID FK→elders | |
| `activity_key` | Text | references `activities_catalog.py` key |
| `call_id` | UUID FK→calls, nullable | |
| `used_at` | timestamptz | |

Index: `(elder_id, used_at desc)` for least-recently-used selection (FR-034 / SC-009). "Recently used" = used within the last 30 days OR among the last 3 activities for the elder (larger exclusion set); when all are excluded, fall back to the least-recently-used overall.

### `family_reports` (UUID PK)
Generated monthly per-elder status-and-trends summary.

| Field | Type | Notes |
|------|------|------|
| `id` | UUID PK | |
| `elder_id` | UUID FK→elders | |
| `period_month` | Date | |
| `report_text` | Text | generated narrative (Vertex; delivered PHI-minimized via SMS) |
| `metrics` | JSONB, default `{}` | trend aggregates (mood/adherence counts) |
| `delivered_sms_id` | UUID FK→sms_messages, nullable | link to the family SMS |
| `created_at` | timestamptz | |

Unique: `(elder_id, period_month)`.

---

## Modified tables

### `call_schedules` (+ evening slot)
- **Add** `slot` Text + CHECK `('morning','evening')`, default `'morning'`.
- **Change** unique constraint `UNIQUE(elder_id)` → `UNIQUE(elder_id, slot)`. Backfill existing rows `slot='morning'`.
- Effect: `get_by_elder` returns a list; materialization iterates per slot; `idempotency_key` becomes `sched:{schedule_id}:{date}` (already per-schedule-row, now naturally per-slot).

### `follow_up_flags` (+ crisis detail)
- **Add** `crisis_category` Text nullable (`suicidal`|`medical`|`abuse`|`confusion`|`overdose`), `detection_source` Text nullable (`llm`|`safety_net`|`both`), `resource_offered` Text nullable (catalog key), `family_notified` Boolean default false.
- A crisis = `severity='urgent'` row with these populated. Existing status machine (`open/acknowledged/resolved`) and PHI-safe `flag.created` webhook unchanged.

### `sms_messages` (+ non-call notifications)
- **Change** `call_id` → nullable.
- **Add** `kind` Text + CHECK `('in_call','family_alert','family_report','opt_out_ack')`, default `'in_call'`.
- **Add** `dedupe_key` Text nullable, unique-where-not-null (e.g. `crisis:{flag_id}`, `missed:{call_id}`) for idempotent family notifications.
- `template_key` becomes nullable (family alerts use system templates, still PHI-minimized).

### `callback_requests` (+ auto-dial lifecycle)
- **Widen** `status` CHECK to add `('scheduled','dialed')` alongside existing `('open','acknowledged','resolved')`.
- **Add** `dispatched_call_id` UUID FK→calls nullable.
- Auto-dial path: `open → scheduled` (on materialize, sets `dispatched_call_id`) → `dialed` (on call start). Spanish callbacks set `profile_override` (existing column on the dial path) to a Spanish profile.

### `elders` (language preference)
- No column change: store `meta["language"]` (e.g. `"es"`), matching the existing `meta["medication_schedule"]` convention. Read into a `preferred_language` builtin.

---

## New builtin variables (resolved API-side in `resolve_builtin_vars`, mirrored in agent `prompt_vars` defaults)

| Builtin | Source | PHI |
|---------|--------|-----|
| `open_family_tasks` | open `family_tasks` for elder | low (task text) |
| `personal_facts` | active `personal_facts` (relevant subset) | yes |
| `last_call_summary` | latest `conversation_summaries.summary` | yes |
| `open_plans` | latest `conversation_summaries.open_plans` | yes |
| `important_dates` | `personal_facts` category=important_date within ±1 day of today | yes |
| `pending_med_reasks` | `medication_reminders` status=pending | yes |
| `survey_due` | no `wellbeing_survey_results` this month → "true" | no |
| `suggested_activity` | resolved on demand via `get_activity` tool (not pre-injected) | no |
| `preferred_language` | `elders.meta["language"]` | no |

All values pass through `prompt_vars.build_vars` sanitization (300-char cap, defaults). Parity contract test asserts every new builtin has a default in the agent mirror.

---

## Entity relationships (text)

- `elders` 1—N `family_contacts`; `family_contacts` 1—N `family_tasks` (also `elders` 1—N `family_tasks`).
- `elders` 1—N `personal_facts`, `conversation_summaries`, `medication_reminders`, `wellbeing_survey_results`, `activity_history`, `family_reports`, `call_schedules` (now ≤2 via slot).
- `calls` 1—1 `conversation_summaries`; 1—N `family_tasks.delivered_call_id`, `wellbeing_survey_results`, `activity_history`.
- `follow_up_flags` gains crisis attributes (still N per elder/call).
- `callback_requests` 1—0..1 `calls` via `dispatched_call_id`.
- `sms_messages` now 0..1 `calls` (nullable) and N per elder; `family_reports` 0..1 `sms_messages`.
