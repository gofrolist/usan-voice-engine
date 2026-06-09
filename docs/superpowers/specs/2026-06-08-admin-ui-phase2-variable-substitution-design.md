# Admin-UI Phase 2 — Dynamic `{{variable}}` Substitution — Design

**Status:** Draft for review · 2026-06-08
**Predecessor:** Phase 1 (`docs/superpowers/plans/2026-06-08-plan-admin-6-ui-redesign.md`, PR #52) made large `{{variable}}`-laden prompts *saveable*. It does **not** resolve them — today the LLM receives the literal text `{{first_name}}`.
**Related:** `docs/superpowers/specs/2026-06-07-admin-ui-design.md` (admin console), `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` (system design, §3 dynamic vars / §7 prompt safety).

---

## 1. Goal

Make `{{variables}}` in agent prompts resolve to real per-call values when the agent speaks, with:

- a **wellness-native built-in catalog** (resolved by code from elder/call/runtime data),
- an **operator-extensible custom tier** (declared in the catalog, fed by the existing `dynamic_vars` dict, no code),
- **warn-don't-block** validation (unknown `{{vars}}` are flagged, never block save — preserving Phase 1's "paste any prompt" win),
- **per-variable defaults** (a missing value falls back to the variable's default, else empty string),
- a **unified `{{ }}` syntax** across every prompt field, including the greeting,
- a **Retell-style insert-variable palette** in the editor.

### Non-goals (deferred)

- A fully data-driven variable registry with admin CRUD and configurable data-source bindings (a later phase; the custom tier covers operator extensibility for now).
- Tool/Functions parity and external integrations (Phase 3).
- Per-field elder overrides (already a deferred non-goal in the admin-ui design).
- New geographic/sales data (`state`, `is_existing_client`, `offer_early_payment`) — no USAN source; out of scope.

---

## 2. Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Catalog anchor | **Hybrid** — wellness-native now, mechanism extensible later |
| 2 | Resolution model | **Two-tier** — built-in (code-resolved) + custom (`dynamic_vars`-fed) |
| 3 | Substitution scope | **All prompt fields**, unified `{{ }}` syntax |
| 4 | Unknown var on save | **Warn, allow save** (don't regress Phase 1) |
| 5 | Missing value at runtime | **Per-variable default, else empty string** |
| 6 | Outbound built-in resolution | **In scope** (USAN's daily calls are outbound) |
| 7 | `{{today_meds}}` built-in | **In scope** (adds an API med lookup at dispatch) |
| 8 | `{{last_mood}}` / `{{last_pain}}` | **In scope** (cheap — same wellness query) |

---

## 3. The variable catalog (v1)

### 3.1 Built-in tier (10 variables, code-resolved)

| Variable | Source | Resolved at | Default |
|---|---|---|---|
| `{{first_name}}` | first whitespace token of `elders.name` | API (dispatch/registration) | `there` |
| `{{elder_name}}` | full `elders.name` (legacy-compat alias) | API | `there` |
| `{{call_direction}}` | `calls.direction` (`inbound`/`outbound`) | API | `""` |
| `{{current_time}}` | wall-clock localized to `elders.timezone` (e.g. `9:15 AM`) | **Agent** (call answer) | `""` |
| `{{current_date}}` | today localized to `elders.timezone` (e.g. `Monday, June 8`) | **Agent** (call answer) | `""` |
| `{{last_check_in}}` | latest `WellnessLog`, raw summary (reuses `_format_last_check_in`) | API | `""` |
| `{{last_check_in_line}}` | legacy pre-formatted sentence ("For context, their last check-in was …") | API | `""` |
| `{{last_mood}}` | latest `WellnessLog.mood` (`1`–`5`) | API | `""` |
| `{{last_pain}}` | latest `WellnessLog.pain_level` (`0`–`10`) | API | `""` |
| `{{today_meds}}` | comma-joined names of today's meds (new lookup) | API | `""` |

`{{current_time}}` / `{{current_date}}` resolve **agent-side at call answer** (not at dispatch), because outbound calls are scheduled/retried and dispatch can precede the conversation by seconds-to-minutes. The agent has no DB, so the API passes `elders.timezone` to it alongside the resolved built-ins.

### 3.2 Custom tier (operator-extensible, no code)

An operator declares a custom variable in the catalog (name + description + optional default) whose value is supplied per call via the **existing** `dynamic_vars` JSONB dict (operator-supplied at `enqueue_call`, capped at 8192 bytes). Adding one needs no code — it just becomes a known `{{var}}` the editor recognizes and the agent resolves from `dynamic_vars`.

A migrated prompt's unsupported variables (`{{offer_early_payment}}`, `{{is_existing_client}}`, …) are either promoted to custom variables (operator supplies values) or left as **warned** tokens that resolve to empty at runtime.

### 3.3 Explicitly excluded

`{{phone}}` (PHI; no reason to speak it), `{{call.call_id}}` (internal ID), `{{state}}` / `{{is_existing_client}}` / `{{offer_early_payment}}` (no USAN data source — sales concepts).

---

## 4. Architecture

### 4.1 Resolution sites

The agent holds **no database**; it reaches data only through `api_client`. Therefore:

- **API resolves all data built-ins** (`first_name`, `elder_name`, `call_direction`, `last_check_in`, `last_check_in_line`, `last_mood`, `last_pain`, `today_meds`) from the loaded `elder`, the latest `WellnessLog`, and a new medications lookup. It also passes `elders.timezone`.
- **Agent resolves runtime built-ins** (`current_time`, `current_date`) locally at call answer, then performs the actual substitution.

### 4.2 Data flow

```
Outbound (calls.py _create_and_dispatch):
  elder + latest WellnessLog + today's meds
    → resolve built-in vars  (NOT written into Call.dynamic_vars)
    → livekit_dispatch.dispatch_agent(job metadata = {operator dynamic_vars, resolved built-ins, timezone})

Inbound (calls.py register_inbound_call):
  elder lookup by phone + latest WellnessLog + today's meds
    → resolve built-in vars
    → InboundCallResponse(dynamic_vars = operator/caller, resolved_vars = built-ins, timezone)

Agent (worker → check_in.build_*):
  merge(resolved built-ins, custom dynamic_vars)
    → add runtime built-ins (current_time/current_date via timezone)
    → apply catalog defaults for missing/empty
    → sanitize caller-derived values (_sanitize_prompt_value)
    → substitute {{ }} across ALL prompt fields
    → Agent(instructions=…)
```

### 4.3 Idempotency separation (critical)

`enqueue_call` uses `Call.dynamic_vars` as part of the **idempotency payload** (`calls.py:49`, `_idempotent_replay` compares `existing.dynamic_vars != body.dynamic_vars`). Server-resolved built-ins **must not** be merged into the persisted `Call.dynamic_vars`, or a replay would falsely 409. Resolved built-ins are computed transiently at dispatch and passed to the agent out-of-band (job metadata / inbound response), never persisted into the idempotency-keyed dict.

### 4.4 Precedence

When a name exists in both tiers, the **built-in value wins** over a same-named `dynamic_vars` key — an operator cannot spoof `first_name` via `dynamic_vars`. Custom variables occupy names that are **not** built-ins. Merge order: `defaults` < `custom dynamic_vars` < `built-ins`.

### 4.5 The substitution engine

A new pure function (agent-side; e.g. `usan_agent/prompt_vars.py`) replaces the single-field `str.format` at `check_in.py:255`:

- **Token-scoped:** only `{{name}}` where `name` is a known catalog variable is replaced. This is *not* `str.format` — arbitrary `{...}` / stray braces in operator text pass through untouched and can never raise `KeyError`/`IndexError` or act as a format-string injection vector.
- **Unknown `{{var}}`** (warned-but-saved, or a value-less custom var with no default): replaced with empty string (its "missing value" path), **not** left as literal braces (so the agent never speaks `{{…}}`).
- **Defaults:** a known variable whose resolved value is `None`/empty uses its catalog default.
- **Caller-derived values** (`dynamic_vars`, elder name, last check-in, med names) keep flowing through `_sanitize_prompt_value` (`check_in.py:40`) **before** substitution — the two-domain safety model is preserved: operator-authored template text is structurally validated; injected values are sanitized.
- **Legacy single-brace compatibility:** the engine also resolves `{elder_name}` and `{last_check_in_line}` (single-brace) **only** for already-published configs, so old `inbound_personalization_template` snapshots still render. New saves from the UI emit `{{ }}` exclusively.

### 4.6 Catalog as the single source of truth

The catalog (name, tier, description, default, example) is a **global constant**, not a per-version snapshot — which sidesteps the forward-compat invariant entirely (variables available at call time are not frozen per published config).

- **API:** authoritative definition (new module, e.g. `usan_api/schemas/variable_catalog.py`).
- **Agent:** a mirrored copy (same pattern as the existing parallel `usan_agent/agent_config.py`), holding names + defaults so it can substitute and default without an API round-trip.
- **Frontend:** fetched at runtime from a new `GET /v1/admin/variable-catalog` (operator-auth) endpoint, so the chip palette and unknown-variable validation never hand-duplicate the list.

---

## 5. Validation

### 5.1 Backend (`apps/api/.../schemas/agent_config.py`)

Brace handling flips from "reject all braces on short fields" to a **uniform token rule** on every prompt field:

- Valid `{{catalog_var}}` tokens are allowed everywhere.
- **Unknown** `{{var}}` tokens are **accepted** (Phase-2 decision: warn, don't block). The server records them (e.g. returns a `warnings` list on the save/validate response) but does not raise.
- **Stray** single `{` or `}` that is not part of a `{{ }}` token (and, on the legacy template, not a recognized single-brace slot) is still **rejected** — typos and malformed `str.format` slots stay caught.
- `inbound_personalization_template`: continues to accept its two legacy single-brace slots on read for back-compat; the UI migrates it to `{{ }}`.

Forward-compat: because unknown tokens are accepted and the catalog is not stored in the config, **no previously-published `agent_profile_versions.config` row can fail validation** under the new rules. No data migration required.

### 5.2 Frontend (`apps/admin-ui/.../config/agentConfigSchema.ts`)

Zod mirrors the backend rule: it rejects stray single braces, accepts `{{ }}` tokens, and surfaces unknown `{{vars}}` as **non-blocking warnings** (not Zod errors) using the catalog fetched from the endpoint.

---

## 6. Admin-UI

- **Insert-variable palette:** a `{}`-style button on the prompt editor (Retell parity) opening a grouped list (Built-in / Custom) of catalog variables; clicking inserts `{{var}}` at the cursor. Hooks into `PromptEditor.tsx` `handleMount` via a Monaco action/widget. The token matcher (`promptTokens.ts` `matchPromptTokens`) and highlight decoration already exist.
- **Token states:** known tokens keep the existing `.prompt-var-token` indigo highlight; **unknown** tokens render in a distinct warn color, and the field shows a small "unknown variable: …" notice listing them.
- **Help text** (`fieldMeta.ts`) updates to point at the palette and explain defaults.

---

## 7. Security

- The two-domain model from the system design (§7) is preserved: operator-authored prompt **structure** is validated (stray-brace rejection, token-scoped substitution), and **injected values** (caller/elder data) are sanitized via `_sanitize_prompt_value` before interpolation.
- Substitution is token-scoped and never calls `str.format` on operator text, eliminating format-string injection and `KeyError` crashes.
- Built-in precedence prevents `dynamic_vars` from spoofing a code-resolved identity variable.
- `{{phone}}` and other PHI-adjacent fields are deliberately excluded from the catalog.

---

## 8. Testing strategy (TDD)

- **Engine unit tests** (`services/agent`): known-var substitution; unknown-var → empty (not literal); per-variable default; precedence (built-in over custom); injection-safety (`{{x}}` with hostile value; stray `{`/`}` passthrough; no `KeyError`); legacy single-brace render; multi-field substitution.
- **API tests** (`apps/api`): built-in resolution for inbound **and** outbound; `today_meds`/`last_mood`/`last_pain` population; `dynamic_vars` idempotency untouched by built-in resolution; `variable-catalog` endpoint shape + auth; lenient validation accepts unknown `{{var}}` and returns warnings; old published config still reads.
- **Frontend tests** (`apps/admin-ui`): catalog fetch → palette renders grouped; click inserts at cursor; unknown-var warning shown but save not blocked; Zod parity (stray brace rejected, `{{ }}` accepted).

---

## 9. File-touch map

**apps/api**
- `schemas/variable_catalog.py` *(new)* — catalog definition + built-in metadata.
- `schemas/agent_config.py` — relax brace validators to the uniform token rule; keep legacy slot acceptance.
- `routers/calls.py` — resolve built-ins for outbound (`_create_and_dispatch`) and inbound (`register_inbound_call`); keep them out of persisted `Call.dynamic_vars`; pass `timezone`.
- `routers/` (admin) — `GET /v1/admin/variable-catalog`.
- a medications lookup (repository) for `{{today_meds}}`.
- `livekit_dispatch.py` — carry resolved built-ins + timezone in job metadata.
- `schemas/call.py` — `InboundCallResponse` carries resolved built-ins + timezone.

**services/agent**
- `prompt_vars.py` *(new)* — the token-scoped substitution engine + catalog mirror + defaults.
- `check_in.py` — replace `_inbound_instructions` `str.format` with the engine; substitute across `checkin_flow_instructions` and the inbound template; resolve runtime clock built-ins.
- `worker.py` — thread resolved built-ins + timezone from the API into the agent builders.
- `agent_config.py` — (mirror only, if catalog defaults live here).

**apps/admin-ui**
- `config/variableCatalog.ts` *(new)* — fetch + types for the catalog endpoint.
- `config/agentConfigSchema.ts` — uniform token rule; unknown-var warnings.
- `features/editor/sections/PromptEditor.tsx` — insert-variable palette + unknown-token styling.
- `features/editor/sections/PromptsSection.tsx` / `fieldMeta.ts` — help text + warning surface.
- `index.css` — warn-token style.

---

## 10. Future (Phase 3 and beyond)

- Fully data-driven variable registry (admin CRUD + configurable source bindings).
- Tool/Functions parity, including external integrations.
- Per-field elder overrides.
