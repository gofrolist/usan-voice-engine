# Contract: New Agent Tool Endpoints (`/v1/tools/*`)

All endpoints follow the existing tool pattern (`apps/api/src/usan_api/routers/tools.py`): POST, authenticated by a per-call service token scoped to `call_id` (minted agent-side, TTL ~300s), request/response are Pydantic v2 models, and `@track_tool("<name>")` records a transcript tool row. Every tool has a matching agent function in `_TOOL_REGISTRY` and a no-op in `_TEST_TOOL_REGISTRY`. Endpoints are additive — no existing tool contract changes.

---

### POST `/v1/tools/raise_crisis`
Records a crisis escalation and returns the resource script for the agent to speak. Called by BOTH the LLM and the deterministic safety-net matcher.

Request:
```json
{ "category": "suicidal|medical|abuse|confusion|overdose",
  "detection_source": "llm|safety_net",
  "evidence": "short non-PHI quote/marker (optional, capped)" }
```
Response:
```json
{ "flag_id": 123,
  "resource_label": "988 Suicide & Crisis Lifeline",
  "resource_number": "988",
  "spoken_script": "I'm really concerned about you. ..." }
```
Behavior: upserts an `urgent` `follow_up_flags` row with crisis columns (`crisis_category`, `detection_source` → `both` if already present from the other path, `resource_offered`); enqueues a `crisis` family alert (idempotent on `crisis:{flag_id}`); resource fields come from `emergency_resources.py`. Idempotent per `(call_id, category)` within a call.

---

### POST `/v1/tools/close_family_task`
Marks an open family task as delivered after the agent conveys it.

Request: `{ "task_id": 456 }`
Response: `{ "status": "delivered" }`
Behavior: `open → delivered`, stamps `delivered_call_id`. (Open tasks are delivered to the agent via the `open_family_tasks` builtin, not fetched by a tool.)

---

### POST `/v1/tools/record_personal_fact`
Captures a durable fact the elder stated during the call.

Request:
```json
{ "category": "person|routine|preference|important_date|health_context",
  "content": "daughter Maria visits on Sundays",
  "structured": { "date": "2026-07-04", "label": "birthday" } }
```
Response: `{ "id": 789 }`
Behavior: inserts a `personal_facts` row with `source='elder_stated'`; if it supersedes an existing active fact (same category+subject heuristic), the old row may be set `active=false` by a later API-side reconcile (extraction step), not by the tool.

---

### POST `/v1/tools/record_survey`
Records the monthly wellbeing survey result.

Request: `{ "loneliness": 2, "mood": 4, "satisfaction": 3, "raw": {} }`
Response: `{ "id": 321, "period_month": "2026-06-01" }`
Behavior: inserts `wellbeing_survey_results` for the current calendar month; unique `(elder_id, period_month)` makes a repeat within the month a no-op/return-existing.

---

### POST `/v1/tools/get_activity`
Returns a mood-boosting activity not used recently.

Request: `{ "kind": "any|breathing|memory|game" }` (optional)
Response: `{ "activity_key": "box_breathing", "title": "...", "script": "..." }`
Behavior: selects the least-recently-used catalog entry (from `activities_catalog.py`) for this elder using `activity_history`, records the use, and returns the script. When all are recently used, returns the least-recently-used overall.

---

### POST `/v1/tools/register_opt_out`
Honors a spoken "stop calling" request.

Request: `{ "reason": "optional short note" }`
Response: `{ "status": "dnc_added" }`
Behavior: adds the elder's number to `dnc_list` (serialized via `lock_phone`); enqueues an `opt_out_ack`/operator notification; future outbound dials are blocked by existing DNC enforcement.

---

### POST `/v1/tools/set_spanish_callback`
Promises and schedules a Spanish-language callback.

Request: `{ "requested_time_text": "esta tarde", "requested_at": "2026-06-14T22:00:00Z" }`
Response: `{ "callback_id": 654 }`
Behavior: sets `elders.meta["language"]="es"`; creates a `callback_requests` row with `profile_override` = the configured Spanish profile; the callback auto-dial poller dials it with Spanish STT/TTS. If no Spanish profile is configured, the request is created and flagged for operator attention.

---

## Behavior changes to an existing tool

### POST `/v1/tools/log_medication` (extended)
When `taken=false`, additionally opens/refreshes a `medication_reminders` row (status `pending`) for that medication. When `taken=true`, clears any `pending` reminder for it. No request/response shape change.

---

## New builtins delivered via dispatch metadata (not tools)
`open_family_tasks`, `personal_facts`, `last_call_summary`, `open_plans`, `important_dates`, `pending_med_reasks`, `survey_due`, `preferred_language` are resolved in `resolve_builtin_vars` and substituted into the prompt — the agent reads them, it does not call a tool to fetch them. A parity contract test asserts each has a default in the agent `prompt_vars` mirror.
