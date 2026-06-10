# Admin-UI Phase 3 — Data-driven tool catalog + wellness tools (design)

**Date:** 2026-06-09
**Status:** Approved (brainstorming) — pending implementation plan
**Predecessors:** Phase 1 (saveable prompts), Phase 2 (`{{variable}}` substitution, merged `ce1961b` / PR #53)
**Related specs:** `docs/superpowers/specs/2026-06-07-admin-ui-design.md`, `docs/superpowers/specs/2026-06-08-admin-ui-phase2-variable-substitution-design.md`

---

## 1. Goal

Move the agent's tool list from a hardcoded 4-item set to a **data-driven catalog**, and add the three wellness tools that are genuinely missing today:

- **`flag_for_followup`** — a safety-escalation flag a human reviews.
- **`schedule_callback`** — records a call-back request (no auto-dialer).
- **`send_sms`** — sends an operator-authored, non-PHI templated text, queued and delivered post-call via Telnyx Messaging.

This is **not** Retell-style arbitrary-webhook "Functions parity." That path was explicitly rejected during brainstorming: USAN is a wellness check-in product, the existing tools + `dynamic_vars` cover today's flow, and operator-defined HTTP functions would let PHI egress to non-BAA endpoints (an SSRF/secret-management/compliance hazard). Phase 3 stays inside the existing PHI boundary, with `send_sms` the one controlled exception (handled in §6).

## 2. Non-goals (this phase)

- Live `transfer_to_human` (SIP REFER) — real telephony lift, deferred to its own phase.
- An admin **review-list UI** for flags/callbacks — API + metric/alert only this phase.
- SMS retry/reconciliation worker — un-sent rows are observable; a worker is a later add.
- Auto-dialer that consumes `callback_requests` (the scheduler is external/unowned).
- Per-elder SMS opt-out field — rely on Telnyx messaging-profile STOP handling + only-enrolled elders.
- Operator-defined / webhook / code-authored tools.

## 3. Architecture

Phase 3 mirrors Phase 2's three-layer pattern with no cross-imports (`apps/api` and `services/agent` never import each other):

| Layer | Role | Phase 3 additions |
|---|---|---|
| `apps/api` | Authoritative: catalog, Pydantic config, tool endpoints, persistence, validation | `TOOL_CATALOG`, `GET /v1/admin/tool-catalog`, 3 new `/v1/tools/*` endpoints, 3 admin `GET` endpoints, 3 tables, Telnyx Messaging client + outbox |
| `services/agent` | Consumes resolved config, exposes `@function_tool`s to the LLM, calls back to the API | 3 new `@function_tool` + `_do_*` helpers + `_TOOL_REGISTRY`/`_select_tools` + `api_client` functions |
| `apps/admin-ui` | Editor; Zod mirror; catalog fetch | `useToolCatalog()`, catalog-driven `ToolsSection`, `send_sms` templates editor |

### 3.1 Deliberate difference from the variable catalog

The tool catalog is a **closed set**. Unknown tool names remain a **hard validation error** (today's `ToolsConfig._known_tools` block), *not* warn-not-block. Variables are open-ended (operators invent custom ones via `dynamic_vars`); tools are a fixed, code-backed inventory.

### 3.2 Forward-compatibility invariant (unchanged from Phase 2)

The catalog is a **global constant, excluded from `agent_profile_versions.config` snapshots**. No migration of historical configs; old published versions keep re-validating. Existing published profiles keep their frozen `tools.enabled` list — the new tools are simply absent and therefore not offered on those calls.

## 4. Data-driven tool catalog

### 4.1 API

New `apps/api/src/usan_api/schemas/tool_catalog.py` (mirror of `schemas/variable_catalog.py`):

```python
class ToolSpec(BaseModel):
    name: str            # registry key, e.g. "flag_for_followup"
    label: str           # human label for the UI
    description: str     # what it does (shown in the editor)
    category: str        # "logging" | "lifecycle" | "safety" | "messaging"
    always_on: bool = False       # end_call: locked on, cannot be disabled
    requires_config: bool = False # send_sms: needs >=1 template to be offered

TOOL_CATALOG: tuple[ToolSpec, ...] = (... 7 tools ...)
TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOL_CATALOG)
```

The 7 entries: `log_wellness`, `log_medication`, `get_today_meds` (logging); `flag_for_followup` (safety); `schedule_callback` (safety); `send_sms` (messaging, `requires_config=True`); `end_call` (lifecycle, `always_on=True`).

`schemas/agent_config.py` imports `TOOL_NAMES` from the catalog (single source of truth) instead of defining its own frozenset. `ToolsConfig._known_tools` validator stays a **block**.

New `apps/api/src/usan_api/routers/admin_tool_catalog.py`: `GET /v1/admin/tool-catalog` (auth `require_admin_session`) → `ToolCatalogResponse(tools=list(TOOL_CATALOG))`. Registered in `main.py` like `admin_variable_catalog`.

### 4.2 Agent mirror

`services/agent/src/usan_agent/check_in.py` `_TOOL_REGISTRY` gains the 3 new callables. `_select_tools` keeps force-including `end_call`; it gains one rule: **`send_sms` is only registered when the resolved config has ≥1 SMS template** (an enabled-but-template-less `send_sms` is a dead tool). Agent-side `ToolsConfig` (`agent_config.py`) mirrors the API default list (no validators).

### 4.3 Admin-UI

- New `apps/admin-ui/src/config/toolCatalog.ts`: `ToolSpec` interface + `useToolCatalog()` (clone of `variableCatalog.ts`, `staleTime: 5m`, `GET /v1/admin/tool-catalog`).
- `agentConfigSchema.ts`: `TOOL_NAMES` const expands to 7; `toolsSchema` keeps `enabled: z.array(z.enum(TOOL_NAMES))` and gains the `sms` sub-schema (§6.1).
- `ToolsSection.tsx`: drop hardcoded `TOOL_HELP`; render toggles + descriptions **from the catalog** (`spec.description`); `end_call` rendered locked-on (`always_on`); `send_sms` shows an "enabled — needs templates" hint when `enabled` includes it but no templates exist.
- `fieldMeta.ts` `tools.enabled` help text de-hardcoded; `send_sms` template fields registered.

### 4.4 Sync test

A test asserts `TOOL_CATALOG` names == agent `_TOOL_REGISTRY` keys (mirror of `test_variable_catalog`'s API↔agent sync assertion), so the two hand-kept copies can't drift.

## 5. New tools: `flag_for_followup` and `schedule_callback`

Both follow the verified `log_wellness` endpoint pattern exactly: `@router.post` + `@track_tool` + `Depends(require_service_token)` + `_authorize_call(body.call_id, claims, db)` + `_require_elder(call)` + repo create + `await db.commit()`, request models extending `ToolCallRequest(call_id)` in `schemas/tools.py`.

### 5.1 `flag_for_followup`

- **`@function_tool`** signature: `flag_for_followup(severity: Literal["routine","urgent"], category: Literal["medical","emotional","medication","safety","other"], reason: str)`. `reason` is PHI-bearing but stays in **our** DB (like `wellness_logs.notes`).
- **Table `follow_up_flags`** (migration 0011): `id BIGSERIAL PK`, `call_id UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE`, `elder_id UUID NOT NULL REFERENCES elders(id)`, `severity TEXT NOT NULL`, `category TEXT NOT NULL`, `reason TEXT` (≤2000), `status TEXT NOT NULL DEFAULT 'open'`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. Index `(elder_id, created_at DESC)`, `(status, created_at DESC)`.
- **Metric** `usan_followup_flags_total{severity, category}` — bounded, PHI-free labels (never `reason`). Counter declared in `observability/custom_metrics.py` next to `TOOL_CALLS_TOTAL`. Ship a Grafana panel + an **alert rule** firing on `severity="urgent"`. *(The notification channel — email/Slack — is a deploy-time config the operator supplies; the rule is shipped, the channel is a deploy step.)*
- **Admin** `GET /v1/admin/follow-up-flags` (router `routers/admin_tools.py`, `require_admin_session`): paginated, filter by `status`/`elder_id`. Returns PHI → audited via `admin_audit.record`.

### 5.2 `schedule_callback`

- **`@function_tool`**: `schedule_callback(requested_time_text: str, requested_at: str | None = None, notes: str | None = None)` — stores the elder's spoken phrasing plus a best-effort ISO timestamp (null when the LLM can't resolve one; no NLP heroics).
- **Table `callback_requests`** (0011): `id BIGSERIAL PK`, `call_id` (CASCADE), `elder_id`, `requested_time_text TEXT NOT NULL`, `requested_at TIMESTAMPTZ` (nullable), `notes TEXT`, `status TEXT NOT NULL DEFAULT 'open'`, `created_at TIMESTAMPTZ DEFAULT now()`. Index `(status, created_at DESC)`.
- **Endpoint** `POST /v1/tools/schedule_callback`; **Admin** `GET /v1/admin/callback-requests`. Counter `usan_callback_requests_total`. **No auto-dialer** — durable requests for a human to action.

## 6. `send_sms`

### 6.1 Config & template model

`ToolsConfig` gains `sms: SmsToolConfig | None = None`:

```python
class SmsTemplate(BaseModel):
    key: str     # LLM-selectable id, e.g. "med_reminder" (slug, unique within profile)
    label: str   # human label
    body: str    # message text; may reference ONLY non-PHI catalog vars

class SmsToolConfig(BaseModel):
    templates: list[SmsTemplate] = Field(default_factory=list)
```

The agent's tool is `send_sms(template_key: str)` — **the LLM selects a key, never writes free text.** This eliminates the LLM-puts-PHI-in-an-SMS path at the source.

### 6.2 PHI enforcement — hard block

An SMS template `body` containing a PHI catalog var (`last_check_in`, `last_check_in_line`, `last_mood`, `last_pain`, `today_meds` — i.e. `PHI_BUILTIN_NAMES`) **fails to save with HTTP 422** (client Zod block + server Pydantic validator). This is **stricter than greetings' warn-only** Phase 2 guardrail, because SMS leaves our system unencrypted and carrier-visible. Reuses the Phase 2 token-detection helpers, applied to `sms.templates[*].body`.

### 6.3 Delivery (queued, post-call)

1. **In-call:** `send_sms(template_key)` → `POST /v1/tools/send_sms`. The API looks up the chosen template in the resolved profile config, **renders the body with the call's non-PHI vars** resolved (`resolve_builtin_vars` restricted to non-PHI names + the runtime clock), and writes an `sms_messages` row `status='pending'` with `to_number` = the elder's number on the call. Returns immediately; the conversation never blocks.
2. **Post-call flush:** a shared `flush_pending_sms(call_id)` runs at call completion — from **both** `complete_call_if_in_progress` (agent `end_call`) and the `room_finished` webhook path — via FastAPI `BackgroundTasks`, so hangup is not delayed. Because the background task runs *after* the response, it **opens its own DB session** (the request session is already closed). It must be **idempotent**: both completion paths can fire for one call, so the flush claims each row by a status-guarded transition (`pending → sent/failed`, mirroring `complete_call_if_in_progress`'s `IN_PROGRESS` gate) so a second flush re-sends nothing. It sends each pending row through `telnyx_messaging.send_sms()` and marks `sent`/`failed`. Gated on `TELNYX_MESSAGING_ENABLED`; when disabled it marks rows `failed` with `error={"reason":"messaging_disabled"}` (observable, not silent).
3. **Reliability trade-off (decided):** **no retry/reconciliation worker this phase** (consistent with the project's "no DB poller" stance). Un-sent rows persist as `pending`/`failed`, surfaced via `GET /v1/admin/sms-messages` + `usan_sms_messages_total{status}` metric + alert.

### 6.4 Table `sms_messages` (0011)

`id UUID PK DEFAULT gen_random_uuid()`, `call_id` (CASCADE), `elder_id`, `to_number TEXT NOT NULL`, `template_key TEXT NOT NULL`, `body TEXT NOT NULL` (rendered), `status TEXT NOT NULL DEFAULT 'pending'` (`pending|sent|failed`), `telnyx_message_id TEXT UNIQUE`, `error JSONB`, `sent_at TIMESTAMPTZ`, `created_at TIMESTAMPTZ DEFAULT now()`, `updated_at TIMESTAMPTZ DEFAULT now()`. Index `(call_id, status)`, `(status, created_at)`.

### 6.5 Telnyx Messaging client (net-new)

New `apps/api/src/usan_api/telnyx_messaging.py`: `async def send_sms(settings, *, to_number, body) -> str` (returns Telnyx message id; raises `TelnyxMessagingError`). Raw `httpx.AsyncClient` (no SDK), Bearer `TELNYX_MESSAGING_API_KEY`, `POST {api_url}/messages` with `messaging_profile_id`, `from`, `to`, `text`. Mirrors `oauth.py`'s httpx + exception pattern.

### 6.6 Settings & infra (net-new)

`settings.py` (after the SIP fields): `telnyx_messaging_api_key: SecretStr | None`, `telnyx_messaging_profile_id: str | None`, `telnyx_from_number: str | None`, `telnyx_messaging_enabled: bool = False`, `telnyx_messaging_api_url: str = "https://api.telnyx.com/v2"`, `telnyx_messaging_timeout_s: int = 10`. Add the blank-able ones to `_blank_to_none`. Mirror into `infra/.env.example`, `infra/.env.prod.example`, `infra/docker-compose.yml` (api service). **Feature flag default false** — SMS never fires until a deploy explicitly enables it. Deploy note: the secret refresh requires a VM `.env` update before the `v*` tag deploy (per the project's deploy mechanics).

### 6.7 Opt-out / compliance

Rely on Telnyx messaging-profile STOP/opt-out handling; only message already-enrolled elders. No per-elder opt-out field this phase.

## 7. Persistence, migration, repos

- **Migration `0011_followup_callback_sms.py`** (`down_revision="0010"`): raw-SQL `op.execute` `CREATE TABLE` + indexes for all three tables; `downgrade` drops in reverse order. Models added to `db/models.py` (auto-discovered by `migrations/env.py`). CASCADE to `calls`, no cascade to `elders`, `server_default` for timestamps/JSONB.
- **Repos** `repositories/follow_up_flags.py`, `callback_requests.py`, `sms_messages.py` follow the `wellness.py` async `add/flush/refresh` + `select` pattern. `sms_messages` adds `get_pending_for_call`, `mark_sent`, `mark_failed`.
- Handlers commit explicitly (`get_db` does not auto-commit). Prometheus increments **after** commit (so a crash can't double-count).

## 8. Defaults & enablement

- **New profiles** (`DEFAULT_AGENT_CONFIG`): `tools.enabled` default list adds **all three** new tools alongside the existing four. `send_sms` is enabled by default but, per §4.2, is only offered to the LLM once a template exists — so a fresh profile with no SMS templates simply doesn't expose it.
- **Existing published versions:** untouched (frozen `enabled`); new tools absent → not offered.

## 9. Security & PHI summary

- `flag_for_followup.reason` / `schedule_callback.notes` are PHI but stay in our Postgres (same trust level as `wellness_logs.notes`); admin read endpoints are session-gated + audited.
- Metric labels are bounded enums only — never `reason`, `notes`, `call_id`, `elder_id`, or phone numbers (PHI-free, mirrors the `end_call` discipline).
- SMS: free text is impossible (template-key only); PHI vars in templates hard-blocked at save; bodies rendered with non-PHI vars only; sending gated behind a feature flag.
- All injected var values continue through `sanitize_prompt_value` (defense-in-depth) before any LLM/SMS use.
- In-call tool endpoints keep `require_service_token` (JWT scoped to `call_id`); admin endpoints keep `require_admin_session` + role checks.

## 10. Testing (TDD)

- **API unit/integration:** each new tool endpoint (auth, `_require_elder`, commit, response); each repo; catalog endpoint; `ToolsConfig` block on unknown tools; SMS template PHI hard-block (422); template render with non-PHI vars; `telnyx_messaging.send_sms` with mocked httpx (success + error); `flush_pending_sms` (idempotent, feature-flag off path, marks sent/failed); metric increments.
- **Agent unit:** new `@function_tool` defs + `_do_*` error-recovery messages; `_TOOL_REGISTRY`/`_select_tools` (incl. the send_sms-needs-template rule and end_call force-include); `api_client` payloads; catalog↔registry sync test.
- **Admin-UI (vitest):** `useToolCatalog`; catalog-driven `ToolsSection` rendering + toggle order + `end_call` locked; `send_sms` templates editor; PHI-in-template client block; the "enabled — needs templates" hint.
- Coverage target ≥80%; `ruff`, `ruff format`, `mypy` (api + agent), `tsc`/`eslint` (admin-ui) all green — CI runs `mypy` too.

## 11. Out of scope (recap)

Live transfer; admin review UI; SMS retry worker; auto-dialer; per-elder opt-out; webhook/operator-defined tools. Each is a clean future increment on top of this catalog.
