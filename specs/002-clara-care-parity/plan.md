# Implementation Plan: Clara Care Parity — Closing the RetellAI Behavioral Gap

**Branch**: `002-clara-care-parity` | **Date**: 2026-06-14 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-clara-care-parity/spec.md`

## Summary

Close the behavioral gap between RetellAI's configured Clara and our self-hosted engine so USAN can cut over without losing functionality. The audit ([research.md](./research.md)) shows the platform already has the hard parts — outbound dialing with DNC/quiet-hours/idempotency, a two-poller schedule→materialize→dial pipeline, a versioned agent profile/prompt system, an agent tool-call framework with API callbacks, per-call wellness/medication/flag/callback logging, outbound SMS via Telnyx, and a structured webhook outbox. The missing behaviors are therefore mostly **additive extensions of established patterns**, not new architecture.

Technical approach by area:

- **Crisis safety (P1)**: add an agent-side deterministic phrase matcher (mirrors the existing `VoicemailWatcher` on the `user_input_transcribed` event) that fires escalation server-side even when the LLM misses; both the matcher and the LLM call one new `raise_crisis` tool. Emergency resource numbers live in a code catalog (mirrors the voice/model catalog-as-code pattern); the tool returns the correct resource script for the agent to speak and records an escalation by extending `follow_up_flags` with crisis columns. Escalation triggers a family alert.
- **Family connection (P2)**: a new `family_contacts` registry, a new Telnyx **inbound** SMS webhook (the first inbound-message handler; HMAC-verified like the LiveKit webhook) that creates `family_tasks` or processes `STOP`, a `convey_family_task` agent tool + a resolved-var injection of open tasks, and an outbound notification path (PHI-minimized SMS) for missed-call and crisis alerts reusing `telnyx_messaging.send_sms`.
- **Medications (P2)**: a `medication_reminders` table holding pending re-asks with an attempt cap; the not-taken branch of `log_medication` opens a re-reminder, a resolved-var surfaces pending re-asks on the next touch, and confirmation clears it.
- **Memory (P2)**: a post-call, **API-side** Vertex summarization step (the agent has no LLM at shutdown) that writes a `conversation_summaries` row and extracts `personal_facts`; an in-call `record_personal_fact` tool for explicit facts; new builtins (`personal_facts`, `last_call_summary`, `open_plans`, `important_dates`) carried forward via `resolve_builtin_vars`.
- **Scheduling (P2/P3)**: add a `slot` discriminator to `call_schedules` and relax `UNIQUE(elder_id)` → `UNIQUE(elder_id, slot)` to support an independent, toggleable evening window; add a callback auto-dial poller phase that clamps `requested_at` via `quiet_hours.next_allowed`, checks DNC, and creates a QUEUED `Call` (`idempotency_key = callback:{id}`) the existing retry poller then dials.
- **Wellbeing (P3)**: a `wellbeing_survey_results` table + a `survey_due` builtin + a `record_survey` tool; an activity catalog-as-code + an `activity_history` table + a `get_activity` tool that returns a least-recently-used activity.
- **Safety education & opt-out (P3)**: anti-scam guidance via prompt content; `register_opt_out` tool and the inbound `STOP` path both add to `dnc_list` and notify family/operator.
- **Language (P3)**: a `set_spanish_callback` tool records the elder's language preference (in `elders.meta`) and creates a callback whose `profile_override` selects a Spanish-configured profile; the auto-dial path dials it with Spanish STT/TTS.

No service-boundary changes: every agent→API interaction stays an HTTP tool/webhook call. Most work is new tools + new API endpoints + ~9 additive tables/columns + three new poller phases, plus prompt content authored in the existing profile editor.

## Technical Context

**Language/Version**: Python 3.14 (`apps/api`, uv), Python 3.12 (`services/agent`, uv). No `apps/admin-ui` work is required for behavioral parity (operator surfaces for the new entities are a thin follow-on; this plan focuses on the call-time behaviors and their APIs).

**Primary Dependencies**: FastAPI + SQLAlchemy (async) + Pydantic v2 + Alembic (api); LiveKit Agents 1.x + Cartesia/Google plugins (agent); `httpx` (already present, Telnyx + inbound). NEW usage: `google-genai`/vertexai in `apps/api` for the post-call summarization/fact-extraction and monthly-report generation (the Vertex path already exists for the admin text-test feature — reuse it). No new third-party services beyond Telnyx inbound messaging (same provider already used for outbound SMS and voice).

**Storage**: PostgreSQL 16 + pgvector. New additive tables: `family_contacts`, `family_tasks`, `personal_facts`, `conversation_summaries`, `medication_reminders`, `wellbeing_survey_results`, `activity_history`, `family_reports`. Additive columns: `call_schedules.slot` (+ relaxed unique), `follow_up_flags` crisis columns (`detection_source`, `resource_offered`, `family_notified`, `crisis_category`), `sms_messages.call_id` made nullable + `kind`, `callback_requests.status` widened (+`scheduled`/`dialed`/`dispatched_call_id`). Catalogs (emergency resources, activities) are **code constants**, not tables.

**Testing**: pytest (api + agent, ≥80% coverage, mypy + ruff in CI). New contract tests for: the inbound-SMS HMAC verifier, the api↔agent tool/var parity for the new tools and builtins, the deterministic crisis matcher (incl. a benign control set for false-positive rate), the callback auto-dial idempotency/quiet-hours clamp, and the evening-slot scheduling.

**Target Platform**: Single GCP Compute Engine VM, Docker Compose. API + agent are containers; LiveKit + livekit-sip run host-networking in prod. No topology change.

**Project Type**: Multi-unit backend service — `apps/api` and `services/agent` evolve in lockstep but do not import each other; all cross-unit flow is HTTP (tool endpoints, webhooks) and LiveKit dispatch metadata.

**Performance Goals**: Crisis escalation + family alert dispatched within 5 minutes of the triggering event (SC-004); deterministic crisis matcher acts within the call (sub-second, in the STT event loop) so resource delivery is immediate; callback auto-dial places the call at the requested time clamped into the legal window; post-call summarization runs asynchronously and is available before the next scheduled call.

**Constraints**: PHI must not egress to non-BAA infra — all summarization/fact-extraction/report generation uses **Vertex AI via ADC**, never the Gemini Developer API; family-facing SMS is **PHI-minimized** (alerts say "please check in," not clinical detail) and rides the same Telnyx channel already carrying voice PHI (assumes Telnyx BAA covers messaging — see research Decision 9 / open confirm). `apps/api` and `services/agent` must not import each other. All new outbound dispatch (callback auto-dial, family SMS) carries an idempotency key and re-checks DNC + quiet hours. New secrets (none expected beyond the already-planned Telnyx messaging vars) must reach the VM `.env` before the deploy tag.

**Scale/Scope**: Thousands of contacts; one daily morning call + optional evening call each; low operator concurrency. ~5 new agent tools, ~8 new/changed API endpoints (tools + inbound webhook + a few admin reads), ~8 new tables + ~5 additive columns, 9 sequential Alembic migrations (0017–0025), 3 new poller phases (callback auto-dial, family-notification outbox, monthly report), 2 code catalogs, and prompt content authored in the existing editor.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.0. Re-checked after Phase 1 design.*

| Principle | Assessment | Status |
|-----------|-----------|--------|
| **I. Service Isolation** | No api↔agent import added. Every new behavior is either an agent tool that calls a new `/v1/tools/*` endpoint, a server-side poller, or an inbound webhook. The deterministic crisis matcher runs agent-side but escalates via the same `raise_crisis` tool endpoint. New builtins are resolved API-side in `resolve_builtin_vars` and travel via dispatch metadata (existing channel); the agent's `prompt_vars` mirror gets the new builtin defaults, guarded by the existing parity contract test. | ✅ PASS |
| **II. PHI Containment (NON-NEGOTIABLE)** | Post-call summarization, personal-fact extraction, and monthly family reports run on **Vertex AI via ADC** (reusing the existing admin text-test Vertex path), not the Gemini Developer API. Personal facts, summaries, survey results, and escalations are stored in the same Postgres as existing PHI. Family-facing SMS is PHI-minimized by template design (`sms_render` already drops PHI builtins; family alerts add no clinical detail). Crisis-escalation webhook payloads keep the existing rule (no `category`/`reason`/`elder_id` pairing). | ⚠️ PASS with mitigation — requires confirming Telnyx BAA covers messaging (research Decision 9) and that report SMS bodies stay PHI-minimized; both enforced in tasks + tests. |
| **III. Type Safety & Validated Contracts** | All new request/response/config bodies are Pydantic v2; all new columns/tables fully typed via `Mapped[...]`; catalogs are typed constants. Inbound Telnyx payloads validated + HMAC-verified at the boundary. mypy + ruff gate in CI. | ✅ PASS |
| **IV. Test-First (NON-NEGOTIABLE)** | Each item lands tests first (RED→GREEN), ≥80% coverage. New tests: inbound HMAC verifier, crisis matcher + benign control set (SC-002 false-positive ceiling), callback idempotency/quiet-hours clamp, evening-slot materialization, tool/var parity for new tools/builtins. | ✅ PASS (commitment, enforced in tasks) |
| **V. Idempotent Outbound Operations** | Callback auto-dial reuses `calls_repo.create_call()` with `idempotency_key = callback:{id}` and re-checks DNC + quiet hours at dial time (existing retry-poller path). Family-notification SMS dedupe on a natural key (e.g., `crisis:{flag_id}` / `missed:{call_id}`). Evening/morning materialization keeps the existing `sched:` key pattern (now per-slot). | ✅ PASS |
| **VI. Observability** | New mutations (crisis raised, opt-out, callback materialized, family SMS sent, summary written) logged as structured JSON with lazy `{}` placeholders; errors propagate (no swallow). Admin audit rows for opt-out/escalation state changes carry no PHI. | ✅ PASS |
| **VII. Simplicity & YAGNI** | Reuses the two-poller dial pipeline, the tool/registry pattern, catalog-as-code, and the existing Telnyx sender. Adds poller *phases* rather than new services; extends `follow_up_flags`/`sms_messages`/`callback_requests` rather than parallel tables where one-to-one; new tables only where cardinality demands it. No admin-ui rework in this plan. | ✅ PASS |
| **Security & Compliance** | Inbound Telnyx webhook HMAC-verified (new verifier mirrors `webhook_signing`), rate-limited, SSRF-irrelevant (inbound). New tool endpoints behind the existing per-call service-token scope. Opt-out is honored both via spoken request and inbound `STOP`. Family numbers are PII stored like elder phones. No new secrets beyond Telnyx messaging vars already in settings. | ✅ PASS |

**Result**: One mitigation flag (PHI via Telnyx SMS + Vertex-only summarization), tracked in Complexity Tracking and Phase 0. No blocking violations; Phase 0 resolves the open confirm.

## Project Structure

### Documentation (this feature)

```text
specs/002-clara-care-parity/
├── plan.md              # This file (/speckit-plan output)
├── spec.md              # Feature spec (/speckit-specify)
├── research.md          # Phase 0 output (grounded decisions)
├── data-model.md        # Phase 1 output (entities, fields, transitions)
├── quickstart.md        # Phase 1 output (validation scenarios)
├── contracts/           # Phase 1 output
│   ├── tools-api.md      # New /v1/tools/* endpoints (agent callbacks)
│   ├── inbound-sms-webhook.md  # Telnyx inbound message contract + HMAC
│   └── admin-api.md      # New admin reads (family contacts/tasks, escalations, reports)
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
apps/api/                                  # FastAPI (Python 3.14, uv)
├── src/usan_api/
│   ├── emergency_resources.py             # NEW — code catalog: 988/911/APS/Poison Control + scripts
│   ├── activities_catalog.py              # NEW — code catalog: breathing/memory/game activities
│   ├── schemas/
│   │   ├── family.py                      # NEW — FamilyContact, FamilyTask request/response
│   │   ├── crisis.py                      # NEW — RaiseCrisisRequest/Response
│   │   ├── personalization.py             # NEW — PersonalFact, ConversationSummary, survey, activity
│   │   └── inbound_sms.py                 # NEW — Telnyx inbound payload model
│   ├── routers/
│   │   ├── tools.py                       # + raise_crisis, convey/close family task, record_personal_fact,
│   │   │                                  #   record_survey, get_activity, register_opt_out, set_spanish_callback
│   │   ├── webhooks.py                    # + POST /webhooks/telnyx (inbound SMS → task / STOP)
│   │   ├── admin_family.py                # NEW — family contacts CRUD + task list
│   │   └── admin_calls.py                 # + crisis-escalation + report read surfaces
│   ├── repositories/
│   │   ├── family_contacts.py             # NEW
│   │   ├── family_tasks.py                # NEW
│   │   ├── personal_facts.py              # NEW
│   │   ├── conversation_summaries.py      # NEW
│   │   ├── medication_reminders.py        # NEW
│   │   ├── survey_results.py              # NEW
│   │   ├── activity_history.py            # NEW
│   │   ├── follow_up_flags.py             # + crisis columns write/read
│   │   └── callback_requests.py           # + status transitions for auto-dial
│   ├── builtin_vars.py                    # + personal_facts, last_call_summary, open_plans,
│   │                                      #   important_dates, open_family_tasks, pending_med_reasks, survey_due
│   ├── telnyx_inbound.py                  # NEW — inbound HMAC verify + parse
│   ├── notifications.py                   # NEW — PHI-minimized family alert/report SMS builder + send
│   ├── summarization.py                   # NEW — Vertex post-call summary + fact extraction (PHI-safe)
│   ├── callback_dialer.py                 # NEW — poller phase: due CallbackRequest → quiet-hours clamp → create_call
│   ├── notification_outbox.py            # NEW — poller phase: flush call_id IS NULL family SMS
│   ├── family_report_job.py              # NEW — monthly per-elder report generation + send
│   ├── schedule_orchestrator.py           # + per-slot materialization (morning + evening)
│   └── settings.py                        # + flags: callback_dialer/notification/report pollers; Telnyx inbound signing key
└── migrations/versions/                   # NEW — sequential, chained, additive (one foundational + per-story for independent delivery)
    ├── 0017_notifications_substrate.py     # sms_messages.call_id nullable + kind + dedupe_key (foundational)
    ├── 0018_followup_crisis_cols.py        # US1 — follow_up_flags crisis columns
    ├── 0019_family_contacts_tasks.py       # US2 — family_contacts + family_tasks
    ├── 0020_medication_reminders.py        # US3 — medication_reminders
    ├── 0021_personal_facts_summaries.py    # US4 — personal_facts + conversation_summaries
    ├── 0022_schedule_slot.py               # US5 — call_schedules.slot + relaxed unique
    ├── 0023_survey_activity.py             # US6 — wellbeing_survey_results + activity_history
    ├── 0024_callback_autodial.py           # US8 — callback_requests status widen + dispatched_call_id
    └── 0025_family_reports.py              # US8 — family_reports

services/agent/                            # LiveKit Agents (Python 3.12, uv)
└── src/usan_agent/
    ├── crisis_watcher.py                  # NEW — deterministic STT phrase matcher (mirrors voicemail.py)
    ├── worker.py                          # + subscribe crisis_watcher on user_input_transcribed
    ├── check_in.py                        # + new tools in _TOOL_REGISTRY and noop_* in _TEST_TOOL_REGISTRY;
    │                                      #   inject new builtins into prompt
    ├── prompt_vars.py                     # + new builtin defaults (mirror)
    └── pipeline.py                        # (unchanged; Spanish handled via callback profile, not mid-call switch)
```

**Structure Decision**: The existing multi-unit layout is retained. New behavior is added by (a) new agent tools that mirror the `log_wellness` shape and call new `/v1/tools/*` endpoints, (b) new API repositories/schemas/routers that mirror the existing wellness/flag/callback modules, (c) additive migrations, (d) three new poller phases co-located with the existing orchestrators, and (e) two code catalogs mirroring the voice/model catalog pattern. The api/agent isolation boundary is preserved; the only net-new external surface is the inbound Telnyx webhook, which is HMAC-verified at the boundary like the existing LiveKit webhook.

## Complexity Tracking

| Item | Why Needed | Simpler Alternative Rejected Because |
|------|------------|-------------------------------------|
| Family-facing SMS may carry PHI-adjacent context over Telnyx | Missed-call and crisis alerts (FR-010/FR-011) inherently signal something about an elder | Telnyx already carries voice PHI for this product; a separate BAA-covered SMS provider doubles vendor/compliance surface. Mitigation: PHI-minimized templates + confirm Telnyx messaging BAA (Phase 0 Decision 9). |
| Post-call summarization is API-side, not agent-side | The agent has no LLM available at session shutdown (research §4); summarization needs an LLM | Doing it agent-side would require holding an LLM client open past the call or a second in-call pass — more cost and complexity; the Vertex path already exists API-side for the admin text-test. |
| New `slot` column instead of a child schedule table | Evening call is a near-identical second window; one discriminator + relaxed unique is the smallest change | A child `call_schedule_slots` table normalizes further but forces dual-claim poller logic and a larger migration for a 2-row-max cardinality. |
