# Phase A4 — Small Unlocks (per-call profile_override, custom variables, per-profile policy) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three independent unlocks that finish seams the codebase already half-built: (A) `profile_override` on ad-hoc `POST /v1/calls` with a watertight idempotency contract, (B) a `custom_variables` table + admin CRUD merged into the variable catalog with server-authoritative PHI enforcement extended to custom names (save/publish/rollback 422 for SMS bodies, warn-don't-block everywhere else), and (C) an optional `policy` section on `AgentConfig` (quiet-hours narrowing within statutory 09:00–21:00 + bounded retry overrides) enforced entirely API-side by generalizing the existing pure functions and re-resolving at every consumption site.

**Architecture:** API: migration `0015` + `CustomVariable` model; new `repositories/custom_variables.py`, `schemas/custom_variables.py`, `routers/admin_custom_variables.py`; touched: `schemas/call.py`, `routers/calls.py`, `repositories/calls.py`, `repositories/agent_profiles.py`, `routers/admin_variable_catalog.py`, `routers/admin_profiles.py`, `schemas/agent_config.py`, `quiet_hours.py`, `retry_policy.py`, `schedule_windows.py`, `schedule_orchestrator.py`, `livekit_dispatch.py`, `observability/custom_metrics.py` (docstring), `main.py`, `tests/conftest.py`. Agent: **zero `src` changes** (one new regression test pinning `extra="ignore"`). admin-ui: new `features/customVariables/`, `features/editor/sections/PolicySection.tsx`; touched: `config/agentConfigSchema.ts`, `config/fieldMeta.ts`, `config/variableCatalog.ts`, `components/NavSidebar.tsx`, `routes.tsx`, `features/editor/ProfileEditorPage.tsx`, `features/editor/PublishDialog.tsx`, `features/editor/hooks.ts`, `features/editor/sections/ToolsSection.tsx`, `features/versions/hooks.ts`, `features/versions/VersionHistoryPage.tsx`, `types/api.ts`. Ships inert: no feature flags (spec §9) — absent `profile_override`, empty `custom_variables`, and absent `policy` all reproduce today's behavior byte-for-byte.

**Tech stack:** Python 3.14 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic raw-SQL, loguru lazy `{}` ids-only), testcontainers Postgres; Python 3.12 agent (pydantic, pytest); React 18 + TypeScript + zod + react-hook-form + @tanstack/react-query + vitest.

**Source spec:** `docs/superpowers/specs/2026-06-10-small-unlocks-design.md` (Approved).
**Format reference:** `docs/superpowers/plans/2026-06-10-plan-outbound-webhooks.md`.

**Executor notes (read before starting):**
1. **Every verify command starts from the repo root** (`/Users/evgenii.vasilenko/gofrolist/usan-voice-engine`). Subagent cwd resets between bash calls — never strip the `cd apps/api && ` / `cd apps/admin-ui && ` prefixes.
2. **Stacked-branch ritual (spec §9):** branch `feat/small-unlocks` starts at the #57 tip `b7d2b8c`; alembic head on the stack is `0014` (`origin/main` is at `0011`). After **each** predecessor (#55 → #56 → #57) squash-merges: `git rebase --onto origin/main <prev-plan-tip> feat/small-unlocks` (first tip to peel: `b7d2b8c`; re-pin after each rebase), re-run the A1 migration roundtrip (a renumber on main breaks `down_revision="0014"`), and re-verify the #55-cited seams (`schemas/schedule.py` / `schemas/batch.py` overrides, `schedule_orchestrator.py:508-510` precedence, `calls.py:307` retry inheritance).
3. **Same-file sequencing (strict — never parallelize tasks sharing a file):** `schemas/agent_config.py` C5 → C6 → D1; `routers/admin_profiles.py` C5 → C6; `repositories/calls.py` B1 → D4 → D6; `repositories/agent_profiles.py` D5 only; `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx` C9 → D11; `tests/conftest.py` A1 only; `db/models.py` A2 only; `main.py` C3 only. **Test files induce the same constraints:** `apps/api/tests/test_agent_config_schema.py` C5 → C6 → D1; `apps/api/tests/test_admin_profiles_custom_vars.py` C5 → C6 → D1 (the policy JSONB round-trip lands there); `apps/api/tests/test_retry_scheduling.py` D4 → D6.
4. House rules: repos take the request session, `flush()+refresh()`, **never commit**; routers commit; raw `op.execute` migrations; loguru lazy `{}` with ids/names only — **never** `dynamic_vars` values, prompt text, or SMS bodies in logs; error messages carry variable **names** and field paths only (spec §7). ruff line-length 100 (selects `S` — no bare `assert` in `src/`); **bare `uv run mypy`** before every push (strict, `files=["src"]`; CI runs it even though CLAUDE.md omits it).
5. **Deliberate decisions made here (record in commit messages where they land):**
   - `schedule_windows.next_run_at` gains keyword-default `policy_start`/`policy_end` and its return type becomes `datetime | None`: `None` is returned **only** for the policy-induced empty intersection (spec §3.3.3 rule 2); statutory-empty still raises `ValueError`. The three pre-existing policy-free call sites (`routers/schedules.py:_compute_next_run_at`, the orchestrator reschedule + `next_occurrence` computations) add an explicit `if … is None: raise ValueError(...)` defensive branch (unreachable without policy kwargs; keeps mypy strict clean without `assert`).
   - Schedule-cadence bookkeeping (`next_run_at` for `next_occurrence` / `record_result`) stays **policy-free**: policy narrows the dial-time clamp/skip only, never the schedule's cadence.
   - **Occurrence-branch two-window rule (the re-claim-loop guard):** the `now < start_utc → rescheduled` staleness check at `schedule_orchestrator.py:307` keys on the **statutory** effective window + days-mask ONLY (exactly today's computation). The policy-narrowed window is resolved *after* that branch and used solely for (a) the empty-intersection `skipped_window` and (b) `dial_at = max(now, eff_start_utc)` / past-`eff_end_utc` skip. Rationale: if the staleness check used policy-narrowed `start_utc`, a schedule claimed at its statutory `next_run_at` (e.g. 09:00) under policy start 09:30 would record `rescheduled` with a policy-free `next_run_at(now) == now` and re-claim every poll cycle until 09:30 — violating the documented "EVERY branch advances next_run_at" invariant (`schedule_orchestrator.py:284-288`) and inflating `MATERIALIZED_CALLS_TOTAL{result="rescheduled"}`. Pinned by `test_schedule_occurrence_policy_push_no_reschedule_loop` (D8).
   - `custom_phi_sms_violations` lives in `schemas/agent_config.py` next to `_reject_phi_in_templates`; violations are dicts `{"loc": ["body","config","tools","sms","templates", <i>, "body"], "msg": …, "type": "value_error.custom_phi_sms"}` raised as `HTTPException(422, detail=[…])` so the client's `tryParseFieldErrors` parses them like pydantic 422s.
   - Batch-target policy∩window=∅ pins strings: `mark_target_skipped(..., reason="window")`, `_materialize_one_target` returns `"skipped_window"` (new bounded value for the existing `MATERIALIZED_CALLS_TOTAL` `result` label). This **invalidates** the `custom_metrics.py` module-docstring claim that `result="skipped_window"` with `source="batch"` is structurally impossible — D8 updates the docstring + the `test_batch_observability.py` comment that cites it (`batch×rescheduled` stays impossible).
   - §6.3's "rollback routes 422s through `mapServerErrors`" is satisfied per-surface: the editor page (save + publish) maps onto form fields; rollback has no form, so `useRollback` parses the same field-error shape and surfaces `msg` via toast — never swallowed. **Exactly one error handler per mutation:** react-query v5 runs a per-`mutate` `onError` *in addition to* the hook-level one — handlers move, they are never duplicated (C9).
   - admin-ui commit scope: confirm with `git log --oneline -- apps/admin-ui | head -5`; default `feat(admin-ui)` / `test(admin-ui)`.
6. TDD discipline: Step 1 writes the failing test, Step 2 **must show RED for the stated reason** before Step 3 implements. Existing-behavior pins (D2/D3 "defaults unchanged") must pass against the *new* keyword-default signatures — write them in Step 1, watch them fail on `ImportError`/`TypeError`, never weaken them.
7. API tests needing an ADMIN session use the `admin_session` fixture (conftest.py:218); operator endpoints use `operator_headers`; migration tests clone `test_ops_queue_migration.py`'s helpers (`_columns`, `_indexes`, `_check_constraints`, `_indexdef`, `_execute`, `_fetch_one`); enqueue tests mock dispatch via the `test_calls.py` pattern (monkeypatch `livekit_dispatch.dispatch_agent` + `dialer.schedule_dial`); runtime tests use the `test_runtime.py` worker-token helper. **admin-ui tests use the repo's route-by-URL `vi.mock("../lib/api")` fake — there is no msw in this repo**, and role gating is asserted through the real `useIsAdmin` query fed by a mocked `/v1/auth/me` (the `NavSidebar.test.tsx:7-8` discipline), never by mocking the hook.

---

## Part A — Migration 0015 + model + contract tests

### Task A1: Migration `0015_custom_variables.py` + conftest TRUNCATE + migration contract test

**Files:**
- Create: `apps/api/migrations/versions/0015_custom_variables.py`
- Modify: `apps/api/tests/conftest.py` (TRUNCATE list, lines 104–110)
- Test: `apps/api/tests/test_custom_variables_migration.py`

- [ ] Step 1: Write the failing test — clone `test_ops_queue_migration.py`'s helpers verbatim:
  - `test_custom_variables_table_shape` — `_columns` asserts: `id`=uuid (default contains `gen_random_uuid`), `name`=text NOT NULL, `description`=text NOT NULL default contains `''`, `example`=text NOT NULL default contains `''`, `phi`=boolean NOT NULL default contains `false`, `created_at`/`updated_at`=timestamptz NOT NULL default contains `now()`; `_check_constraints` contains `ck_custom_variables_name_slug`; `_indexes` contains the unique index on `name` (`custom_variables_name_key`).
  - `test_slug_check_enforced` — raw `_execute` INSERTs, each `pytest.raises((IntegrityError, DBAPIError))`: `"Bad"`, `"9starts_with_digit"`, `"has space"`, `"has-dash"`, `"a" * 65`; then `"ok_name_1"` inserts fine.
  - `test_unique_name_enforced` — insert `"pet_name"` twice → second raises.
  - `test_downgrade_upgrade_roundtrip` — `subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0014"], ...)` (conftest env dict) → table gone (`_columns` returns `{}`); `upgrade head` → table + slug CHECK back. **Always finishes at head; runs last in the module.**
- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_custom_variables_migration.py -v
```
RED reason: alembic head is `0014`; `_columns` returns `{}` → `KeyError: 'id'`.

- [ ] Step 3: Implement `0015_custom_variables.py` — header mirrors 0014 (`revision="0015"`, `down_revision="0014"`, raw `op.execute`); SQL verbatim from spec §4 **including the comment block** (definitions are documentation/UX only — values arrive per call via `Call.dynamic_vars`; `name` immutable after create; builtin-collision enforced in the Pydantic layer):

```sql
CREATE TABLE custom_variables (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT '',
    phi BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_custom_variables_name_slug CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$')
);
```
  `downgrade()`: `DROP TABLE IF EXISTS custom_variables`. Edit `tests/conftest.py` `_truncate_and_dispose`: prepend `"TRUNCATE custom_variables, webhook_deliveries, …"` (no FK children — position is free; keep it first for clarity).

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_custom_variables_migration.py -v && uv run ruff check migrations tests && uv run ruff format --check migrations && uv run mypy
```
- [ ] Step 5: `git add apps/api/migrations/versions/0015_custom_variables.py apps/api/tests/test_custom_variables_migration.py apps/api/tests/conftest.py && git commit -m "feat(api): migration 0015 — custom_variables catalog table (slug CHECK, unique immutable name)"`

---

### Task A2: `CustomVariable` SQLAlchemy model

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (append after `WebhookDelivery`)
- Test: `apps/api/tests/test_custom_variable_model.py`

- [ ] Step 1: Write the failing test (pure `__table__` introspection, `test_batch_models.py` pattern):
  - `test_custom_variable_columns_and_defaults` — `__tablename__ == "custom_variables"`; column set == `{id, name, description, example, phi, created_at, updated_at}`; `name` not nullable and `unique is True`; `phi` server_default arg contains `"false"`; `description`/`example` server_default present; `updated_at.onupdate is not None`.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_custom_variable_model.py -v` — RED: `ImportError: cannot import name 'CustomVariable'`.
- [ ] Step 3: Implement — model mirroring the house pattern (UUID pk `server_default=text("gen_random_uuid()")`, `Text` + `server_default("''")`, created/updated pattern). Docstring carries the spec §4 contract: *definitions are documentation/UX only; values flow via `Call.dynamic_vars`; `name` is immutable after create (rename would silently orphan `{{tokens}}`); builtin-collision authority stays in the Pydantic layer.*
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_custom_variable_model.py tests/test_custom_variables_migration.py -v && uv run ruff check src/usan_api/db/models.py && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/db/models.py apps/api/tests/test_custom_variable_model.py && git commit -m "feat(api): CustomVariable ORM model (migration 0015 mirror)"`

---

### Task A3: Part A gate

- [ ] `cd apps/api && uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy` — full suite green (proves the conftest TRUNCATE change broke nothing). Commit stragglers as `chore(api): Part A gate fixes` (explicit `git add`).

---

## Part B — `profile_override` on `POST /v1/calls` (Feature A)

### Task B1: Request/response schema + `create_call` kwarg threading

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py` (`CreateCallRequest`, `CallResponse.from_model`), `apps/api/src/usan_api/repositories/calls.py` (`create_call`, **first of three sequential edits**: B1 → D4 → D6)
- Test: `apps/api/tests/test_calls_profile_override.py` (new; all Feature A tests live here)

- [ ] Step 1: Write the failing tests:
  - `test_create_call_request_profile_override_optional_uuid` — `CreateCallRequest(elder_id=…, idempotency_key="k")` → `profile_override is None`; with `profile_override=<uuid>` → typed `uuid.UUID`; `profile_override="not-a-uuid"` → `pytest.raises(ValidationError)`.
  - `test_create_call_persists_profile_override` — direct-session repo test (the `test_call_batches_repo.py` `session_factory` pattern): `calls_repo.create_call(db, …, profile_override=pid)` → row's `profile_override == pid`; omitted kwarg → `None` (column exists since migration 0010 — only the kwarg is new).
  - `test_call_response_echoes_profile_override` — `CallResponse.from_model(call)` where `call.profile_override = pid` → `resp.profile_override == pid`; `None` stays `None`.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_calls_profile_override.py -v` — RED: `ValidationError`/`AttributeError: profile_override` on the schema, `TypeError: unexpected keyword argument 'profile_override'` on the repo.
- [ ] Step 3: Implement — `CreateCallRequest.profile_override: uuid.UUID | None = None` (comment: validated live on the create path only; part of the idempotency payload contract, spec §3.1); `CallResponse.profile_override: uuid.UUID | None` populated in `from_model` (comment: echo for operator-system day-2 triage; A2-console column deferred, Open Q6); `create_call(…, profile_override: uuid.UUID | None = None)` → `Call(profile_override=profile_override, …)`.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_calls_profile_override.py tests/test_calls.py tests/test_calls_lifecycle.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/call.py apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_calls_profile_override.py && git commit -m "feat(api): CreateCallRequest/CallResponse profile_override + create_call kwarg threading"`

---

### Task B2: `enqueue_call` wiring — liveness 422, idempotency contract, DNC path, runtime e2e

**Files:**
- Modify: `apps/api/src/usan_api/routers/calls.py` (`_idempotent_replay` lines 35–42, `enqueue_call` lines 107–139, `_create_and_dispatch` create_call call site)
- Test: `apps/api/tests/test_calls_profile_override.py` (append)

- [ ] Step 1: Write the failing tests (dispatch mocked per the `test_calls.py` pattern):
  - `test_enqueue_with_live_override_persists_and_echoes` — seed an ACTIVE+published profile (via `agent_profiles` repo: create → publish); `POST /v1/calls` with `profile_override` → 202, response `profile_override` echoed, DB row carries it (fresh-session read).
  - `test_enqueue_422_when_override_not_live` — parametrized over three non-live shapes: profile never published (draft only), profile archived (raw `UPDATE agent_profiles SET status='archived'`), random UUID → 422 with detail exactly `"profile_override must reference an active profile with a published version"` (the `_OVERRIDE_ERROR` text, `routers/batches.py:49`).
  - `test_dnc_path_persists_override` — elder's number on DNC; enqueue with live override → 200, row status `dnc_blocked`, `profile_override` persisted **and** echoed (the DNC branch passes the kwarg too).
  - `test_replay_identical_after_override_archived_returns_200` — **the ordering pin (spec §3.1, load-bearing):** enqueue with live override → raw-UPDATE the profile to `archived` → replay the byte-identical request → **200** with the original call (replay pre-check beats liveness; never 422).
  - `test_replay_with_different_override_409` — parametrized: (original `None` → replay with override), (original override A → replay override B) → 409 `"idempotency_key reused with a different payload"`; identical override → 200.
  - `test_idempotent_replay_helper_409_on_override_mismatch` — **unit test on `_idempotent_replay` directly** (fabricated `Call` + `CreateCallRequest` + `Response()`): override mismatch raises 409; this covers BOTH consumers — the pre-check (`calls.py:123`) and the IntegrityError race fallback (`calls.py:70`) — since both call the same helper.
  - `test_runtime_config_resolves_adhoc_override_end_to_end` — enqueue with override of a published profile → `GET /v1/runtime/agent-config?direction=outbound&call_id=<id>` (worker token, `test_runtime.py` helper) → `profile_id == override`, `source == "resolved"` (pins the existing `runtime.py` precedence walk against the new write path).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_calls_profile_override.py -v` — RED: no 422 (validation missing), replay-mismatch returns 200 (helper doesn't compare overrides), DB row `profile_override` is `None` (kwarg not passed).
- [ ] Step 3: Implement in `routers/calls.py`:
  - `_idempotent_replay` mismatch check becomes `existing.elder_id != body.elder_id or existing.dynamic_vars != body.dynamic_vars or existing.profile_override != body.profile_override` (spec §3.1 snippet verbatim).
  - Module-level `_OVERRIDE_ERROR = "profile_override must reference an active profile with a published version"` + `async def _require_live_override(db, profile_id)` — same wrapper shape as `routers/schedules.py:69-75`, raising 422 with `_OVERRIDE_ERROR`.
  - In `enqueue_call`, **after** the replay pre-check (`calls.py:123`) and **before** the DNC branch: `if body.profile_override is not None: await _require_live_override(db, body.profile_override)` — one check covers both create branches; comment states the ordering contract (identical replay must win even when the profile was archived since — the retry-on-timeout contract) and the auth-tier note (operator-token scope; grants no new authority, spec §7).
  - Pass `profile_override=body.profile_override` at **both** `create_call` sites (DNC branch in `enqueue_call`, dispatch branch in `_create_and_dispatch`).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_calls_profile_override.py tests/test_calls.py tests/test_runtime.py tests/test_retry_scheduling.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/routers/calls.py apps/api/tests/test_calls_profile_override.py && git commit -m "feat(api): profile_override on POST /v1/calls — replay-first ordering, liveness 422, idempotency payload match, DNC persistence"`

---

## Part C — Custom variable definitions (Feature B)

### Task C1: `repositories/custom_variables.py`

**Files:**
- Create: `apps/api/src/usan_api/repositories/custom_variables.py`
- Test: `apps/api/tests/test_custom_variables_repo.py`

- [ ] Step 1: Write the failing test (direct `session_factory` pattern):
  - `test_create_list_get_update_delete_roundtrip` — create two (`zebra_var`, `apple_var`); `list_custom_variables` returns **alphabetical** (`apple_var` first); `get_custom_variable` by id; `update_custom_variable(db, row, description=…, example=…, phi=True)` mutates only those three; `delete_custom_variable` removes; list empty.
  - `test_create_duplicate_raises_domain_error` — second `create_custom_variable(name="pet_name")` → `pytest.raises(DuplicateCustomVariableError)` (repo catches `IntegrityError` on flush; SAVEPOINT-wrapped so the session stays usable).
  - `test_names_and_phi_names_helpers` — seed `pet_name` (phi=False), `diagnosis` (phi=True) → `await names(db) == frozenset({"pet_name", "diagnosis"})`, `await phi_names(db) == frozenset({"diagnosis"})`; empty table → both `frozenset()`.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_custom_variables_repo.py -v` — RED: `ModuleNotFoundError: usan_api.repositories.custom_variables`.
- [ ] Step 3: Implement (flush-only, never commit): `class DuplicateCustomVariableError(Exception)` (message user-facing, returned in the 409 body); `create_custom_variable(db, *, name, description, example, phi) -> CustomVariable` (begin_nested + flush, IntegrityError → domain error); `get_custom_variable`, `list_custom_variables` (`ORDER BY name`), `update_custom_variable` (description/example/phi only — **no name parameter exists**, immutability by construction), `delete_custom_variable`; `names(db) -> frozenset[str]`, `phi_names(db) -> frozenset[str]` (single-column SELECTs — the save-path fetch, spec §3.2).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_custom_variables_repo.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/custom_variables.py apps/api/tests/test_custom_variables_repo.py && git commit -m "feat(api): custom_variables repo — CRUD + names/phi_names catalog fetches, immutable name by construction"`

---

### Task C2: `schemas/custom_variables.py`

**Files:**
- Create: `apps/api/src/usan_api/schemas/custom_variables.py`
- Test: `apps/api/tests/test_custom_variable_schemas.py`

- [ ] Step 1: Write the failing test:
  - `test_create_accepts_valid_slug` — `CustomVariableCreate(name="pet_name", description="", example="", phi=False)` validates; defaults: `description=""`, `example=""`, `phi=False`.
  - `test_create_rejects_bad_slugs` — parametrized `ValidationError`: `"Bad"`, `"9name"`, `"has space"`, `"has-dash"`, `"a"*65`, `""`.
  - `test_create_rejects_builtin_collision` — parametrized over all 10 `BUILTIN_NAMES` (import from `variable_catalog`) → `ValidationError` whose message names the builtin tier (authority stays in code, spec §3.2).
  - `test_create_caps_description_and_example` — description > 500 chars → `ValidationError`; example > 200 → `ValidationError`.
  - `test_update_has_no_name_field_and_forbids_extras` — `"name" not in CustomVariableUpdate.model_fields`; `CustomVariableUpdate.model_validate({"name": "x"})` → `ValidationError` (`extra="forbid"` — the 422-on-name-change-attempt contract); empty body validates (all-optional).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_custom_variable_schemas.py -v` — RED: module missing.
- [ ] Step 3: Implement: `CustomVariableCreate {name: str (pattern ^[a-z][a-z0-9_]{0,63}$), description: str = "" (max 500), example: str = "" (max 200), phi: bool = False}` with a `field_validator("name")` rejecting `name in BUILTIN_NAMES`; `CustomVariableUpdate {description?, example?, phi?}` with `model_config = ConfigDict(extra="forbid")`; `CustomVariableOut {id, name, description, example, phi, created_at, updated_at}` + `from_model`.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_custom_variable_schemas.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/custom_variables.py apps/api/tests/test_custom_variable_schemas.py && git commit -m "feat(api): custom-variable schemas — slug + builtin-collision validators, immutable-name PATCH shape"`

---

### Task C3: `routers/admin_custom_variables.py` + `main.py` registration

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_custom_variables.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router` after `admin_variable_catalog`)
- Test: `apps/api/tests/test_admin_custom_variables_api.py`

- [ ] Step 1: Write the failing test (mirror `test_admin_users_api.py`; `client` + `admin_session`):
  - `test_list_requires_session` — `GET /v1/admin/custom-variables` no cookie → 401.
  - `test_create_and_list_alphabetical` — POST two (`zebra_var`, `apple_var`) → 201 each with full `CustomVariableOut` echo; GET list → `apple_var` first.
  - `test_create_duplicate_409`, `test_create_bad_slug_422`, `test_create_builtin_collision_422` (`elder_name`).
  - `test_patch_edits_description_example_phi` — PATCH `{"phi": true, "description": "d2"}` → 200, fields updated, `name` unchanged.
  - `test_patch_rejects_name_422` — PATCH `{"name": "other"}` → 422 (extra forbidden).
  - `test_patch_unknown_404`, `test_delete_unknown_404`, `test_delete_204`.
  - `test_viewer_cannot_mutate_403` — viewer-role session (the `test_admin_users_api.py:65` `async_database_url` recipe): POST/PATCH/DELETE → 403; GET list → 200.
  - `test_mutations_audited_with_phi_old_new_detail` — after create/patch(phi flip)/delete, `admin_audit_log` rows exist with `actor_email` = session email, actions `custom_variable.create|update|delete`, `entity_type="custom_variable"`; the update row's `detail` contains `{"phi": {"old": False, "new": True}}` plus the changed-field names; **no `dynamic_vars`/values anywhere in detail** (spec §5).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_custom_variables_api.py -v` — RED: 404 (router unregistered).
- [ ] Step 3: Implement — copy the `admin_users.py` precedent exactly (spec §5): router `prefix="/v1/admin/custom-variables"`, router-level `Depends(require_admin_session)`, write routes additionally `Depends(require_admin_role(AdminRole.ADMIN))` + `Depends(get_actor_email)`; `GET ""` list (alphabetical, all-session-roles); `POST ""` → repo create, `DuplicateCustomVariableError` → rollback + 409; `PATCH "/{variable_id}"` → 404 unknown, apply present fields, audit detail = changed-field names + `{"phi": {"old": …, "new": …}}` when phi changed (flips allowed both directions, silently but audited — spec §5); `DELETE "/{variable_id}"` → 404 unknown, hard delete (no referential scan — tokens revert to unknown-warnings, spec §4); every mutation: `admin_audit.record(...)` **before** the single `db.commit()`; private `_to_out` mapper. Register in `main.py` after `admin_variable_catalog`. Existing `/v1/admin` rate limiting covers the prefix (`ratelimit.py:42`) — no ratelimit edits.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_custom_variables_api.py tests/test_admin_users_api.py tests/test_app_security.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/routers/admin_custom_variables.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_custom_variables_api.py && git commit -m "feat(api): /v1/admin/custom-variables CRUD — ADMIN-gated writes, phi old/new audit detail"`

---

### Task C4: DB-backed variable-catalog merge

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_variable_catalog.py`
- Test: `apps/api/tests/test_variable_catalog_api.py` (append)

- [ ] Step 1: Write the failing tests:
  - `test_catalog_merges_customs_after_builtins` — create customs `zebra_var`, `apple_var` (phi=True) via the C3 API → GET catalog: first 10 entries are the builtins in canonical order (reuse the existing order assertion), then `apple_var`, `zebra_var` (alphabetical); each custom: `tier == "custom"`, `default == ""`, `phi` echoed.
  - `test_catalog_empty_table_identical_to_builtin_constant` — no customs → response JSON `variables` == the exact serialization of `BUILTIN_VARIABLES` (ship-inert pin, spec §9).
  - `test_builtin_shadowed_custom_dropped_and_logged` — raw-SQL insert a `custom_variables` row named `elder_name` (bypasses the C2 validator — the *future-builtin* scenario, spec §3.2); GET catalog → exactly one `elder_name`, `tier == "builtin"`; loguru capture contains `"custom variable {name} shadowed by builtin; ignored"`-shaped warning bound with the name only.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_variable_catalog_api.py -v` — RED: customs absent from the response.
- [ ] Step 3: Implement — `get_variable_catalog(db: AsyncSession = Depends(get_db))` (the route gains the `db` dependency it lacks today): `customs = await custom_variables_repo.list_custom_variables(db)`; drop any `c.name in BUILTIN_NAMES` with a `logger.bind(name=c.name).warning(...)`; map survivors to `VariableSpec(name=…, tier="custom", description=…, default="", example=…, phi=…)`; return `list(BUILTIN_VARIABLES) + customs`. Update the docstring: now DB-backed; **definitions carry no values** (spec §3.2).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_variable_catalog_api.py tests/test_variable_catalog.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/routers/admin_variable_catalog.py apps/api/tests/test_variable_catalog_api.py && git commit -m "feat(api): variable catalog merges custom tier (default empty, builtin-shadowed customs dropped + logged)"`

---

### Task C5: Save-path warnings pick up declared customs (`unknown_tokens` wiring + `phi_names` generalization)

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (**first of three sequential edits**: C5 → C6 → D1), `apps/api/src/usan_api/routers/admin_profiles.py` (**first of two**: C5 → C6)
- Test: `apps/api/tests/test_agent_config_schema.py` (append), `apps/api/tests/test_admin_profiles_custom_vars.py` (new)

- [ ] Step 1: Write the failing tests:
  - In `test_agent_config_schema.py`: `test_phi_tokens_in_sensitive_fields_accepts_custom_phi_names` — `phi_tokens_in_sensitive_fields(prompts, phi_names=PHI_BUILTIN_NAMES | {"diagnosis"})` flags `{{diagnosis}}` in `voicemail_message` with the existing message shape; `test_phi_tokens_default_unchanged` — call with no kwarg reproduces today's builtin-only output on the same prompts (zero-diff pin).
  - In `test_admin_profiles_custom_vars.py` (`client` + `admin_session`): `test_declared_custom_absent_from_unknown_warnings` — create custom `pet_name`; PUT draft with `{{pet_name}}` in `greeting` → 200, `warnings` does **not** contain `"pet_name"`; `test_undeclared_token_still_warns` — `{{mystery}}` → `"mystery"` in `warnings`; `test_custom_phi_in_sensitive_field_warns` — custom `diagnosis` phi=True, `{{diagnosis}}` in `voicemail_message` → a warning naming `{{diagnosis}}` and `'voicemail_message'` (warn-don't-block: still 200).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_agent_config_schema.py tests/test_admin_profiles_custom_vars.py -v` — RED: `TypeError: unexpected keyword argument 'phi_names'`; `"pet_name"` present in warnings; no custom-PHI warning.
- [ ] Step 3: Implement:
  - `phi_tokens_in_sensitive_fields(prompts, *, phi_names: frozenset[str] = PHI_BUILTIN_NAMES)` — replace the `PHI_BUILTIN_NAMES` membership check with `phi_names` (keyword default = zero-diff for all existing callers/tests, spec §3.2). `unknown_tokens` already takes `known_names` — no change.
  - `routers/admin_profiles.py` `update_draft`: fetch once — `custom_names = await custom_variables_repo.names(db)`, `custom_phi = await custom_variables_repo.phi_names(db)`; pass `unknown_tokens(text, known_names=custom_names)` and `phi_tokens_in_sensitive_fields(prompts, phi_names=PHI_BUILTIN_NAMES | custom_phi)`. Comment: the prompt channel has **no** fail-closed defense (the agent substitutes `dynamic_vars` into all prompt fields) — the warning *is* the defense (spec §3.2).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_agent_config_schema.py tests/test_admin_profiles_custom_vars.py tests/test_admin_profiles_api.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/agent_config.py apps/api/src/usan_api/routers/admin_profiles.py apps/api/tests/test_agent_config_schema.py apps/api/tests/test_admin_profiles_custom_vars.py && git commit -m "feat(api): save-path warnings know declared customs — unknown_tokens wiring + phi_names generalization"`

---

### Task C6: Custom-PHI-in-SMS — shared 422 helper at save/publish/rollback + renders-empty warnings + send-time pin

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (second sequential edit), `apps/api/src/usan_api/routers/admin_profiles.py` (second sequential edit)
- Test: `apps/api/tests/test_agent_config_schema.py` (append), `apps/api/tests/test_admin_profiles_custom_vars.py` (append), `apps/api/tests/test_sms_render.py` (append)

- [ ] Step 1: Write the failing tests:
  - Unit (`test_agent_config_schema.py`): `test_custom_phi_sms_violations_exact_loc` — config dict with templates `[clean, "{{diagnosis}}"]`, `phi_names={"diagnosis"}` → exactly one violation with `loc == ["body","config","tools","sms","templates",1,"body"]` and `msg` naming `{{diagnosis}}`; `test_custom_phi_sms_violations_clean_and_absent_tools` — clean templates / `tools.sms` absent / `tools` absent → `[]`; builtin PHI names are NOT this helper's job (pydantic blocks them earlier — pin by passing `phi_names=frozenset()` over a builtin-PHI-free body).
  - API (`test_admin_profiles_custom_vars.py`): `test_save_422_custom_phi_in_sms_body` — custom `diagnosis` phi=True; PUT draft with SMS template body `"Your result: {{diagnosis}}"` → 422; `body["detail"][0]["loc"] == ["body","config","tools","sms","templates",0,"body"]`; **draft unchanged** (fresh GET shows the prior config — check runs before persistence).
  - `test_publish_422_after_phi_flip` — save draft with `{{x}}` in an SMS body while `x` is phi=False (200 + renders-empty warning) → PATCH `x` to phi=True → `POST /{id}/publish` → 422 from the helper.
  - `test_rollback_422_when_snapshot_references_now_phi_custom` — publish v1 with `{{x}}` in an SMS body (x non-PHI), publish a clean v2, flip `x` phi=True → `POST /{id}/rollback/1` → **422** (the no-pydantic-re-entry hole, spec §3.2.1: `repo.rollback → repo.publish` republishes the old snapshot otherwise); rollback to the clean v2 → 201.
  - `test_save_warns_renders_empty_for_non_phi_custom_in_sms` — non-PHI custom `pet_name` in an SMS body → 200, `warnings` contains exactly `'{{pet_name}} is not substituted in SMS — it will render as empty text.'`; same warning for an **undeclared** `{{mystery2}}` (declared-vs-undeclared parity — hard-blocking only declared would be perverse, spec §3.2.1); a builtin non-PHI token (`{{first_name}}`) produces **no** such warning.
  - Send-time pin (`test_sms_render.py`): `test_custom_tokens_render_empty_even_with_value_in_dynamic_vars` — body `"Hi {{pet_name}}!"`, `call.dynamic_vars == {"pet_name": "PHIPHI-Rex"}` → `render_sms_body(...) == "Hi !"` and the sentinel is absent (regression-pins the fail-closed invariant: `dynamic_vars` never enters the SMS substitution map, spec §3.2.1 fact 2 — so a phi flip after publish is safe immediately).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_agent_config_schema.py tests/test_admin_profiles_custom_vars.py tests/test_sms_render.py -v` — RED: `ImportError: custom_phi_sms_violations`; save/publish/rollback return 200/201; warning absent. (The send-time pin may already be GREEN — that is acceptable for a regression pin; verify it by mutating `render_sms_body` mentally, not by weakening it.)
- [ ] Step 3: Implement:
  - In `schemas/agent_config.py`: `def custom_phi_sms_violations(config: dict[str, Any], phi_names: frozenset[str]) -> list[dict[str, Any]]` — walks `config["tools"]["sms"]["templates"]` (tolerant of absent keys / None), `_TOKEN_RE.findall(body)` ∩ `phi_names` → one violation per offending template, loc fabricated exactly `["body","config","tools","sms","templates", i, "body"]`, `msg` mirroring `_reject_phi_in_templates`'s text, `type="value_error.custom_phi_sms"`. Docstring: PRIMARY enforcement for customs (client shows only a notice, spec §6.3); field-level loc is load-bearing; the pydantic validators keep blocking the 5 builtins unchanged. Also add `def sms_renders_empty_warnings(tools: ToolsConfig | None) -> list[str]` — every non-`BUILTIN_NAMES` token across SMS bodies, de-duplicated first-seen, formatted as pinned above.
  - In `routers/admin_profiles.py`: **save** — before `repo.update_draft`, `violations = custom_phi_sms_violations(body.config.model_dump(), custom_phi)`; non-empty → `raise HTTPException(422, detail=violations)`; append `sms_renders_empty_warnings(body.config.tools)` (minus names already 422-blocked — unreachable, but keep the order: 422 first) to `warnings`. **publish** — fetch `custom_phi`, run the helper on `profile.draft_config` before `repo.publish`. **rollback** — fetch the target via `repo.get_version` (404 if missing), run the helper on `target.config` before `repo.rollback`. Comment on rollback: clone-from copies only a draft (no publish) — next save/publish catches it; accepted (spec §3.2.1).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_profiles_custom_vars.py tests/test_admin_profiles_api.py tests/test_agent_config_schema.py tests/test_sms_render.py tests/test_tools.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/agent_config.py apps/api/src/usan_api/routers/admin_profiles.py apps/api/tests/test_agent_config_schema.py apps/api/tests/test_admin_profiles_custom_vars.py apps/api/tests/test_sms_render.py && git commit -m "feat(api): custom-PHI SMS 422 at save/publish/rollback via shared helper + renders-empty warnings + send-time invariant pin"`

---

### Task C7: admin-ui — Custom Variables page, nav, query invalidation

**Files:**
- Create: `apps/admin-ui/src/features/customVariables/CustomVariablesPage.tsx`, `apps/admin-ui/src/features/customVariables/hooks.ts`
- Modify: `apps/admin-ui/src/routes.tsx`, `apps/admin-ui/src/components/NavSidebar.tsx` (the `Config` GROUPS entry), `apps/admin-ui/src/config/variableCatalog.ts` (stale comment, lines 23–24)
- Test: `apps/admin-ui/src/test/CustomVariablesPage.test.tsx` (new), `apps/admin-ui/src/test/NavSidebar.test.tsx` (append), `apps/admin-ui/src/test/VariablePalette.test.tsx` (append if not already covered)

- [ ] Step 1: Write the failing tests (route-by-URL `vi.mock("../lib/api")` fake per the existing AdminUsers/Calls page test pattern — **no msw in this repo**):
  - `CustomVariablesPage.test.tsx`: `renders table with name, description, example and PHI badge` (two mocked variables, one phi); `create dialog posts and invalidates both query keys` — submit → POST `/v1/admin/custom-variables` body asserted; spy on `queryClient.invalidateQueries` (or assert refetch) for **both** `["custom-variables"]` and `["variable-catalog"]` (the 5-min `staleTime` assumption, spec §6.1); `create dialog shows the PHI help text` — asserts the exact spec §6.1 copy ("never put PHI in them … PHI variables are blocked in SMS templates"); `delete confirms then DELETEs`; `mutation buttons hidden for viewer role` — serve `/v1/auth/me` with `role: "viewer"` through the api mock and let the **real `useIsAdmin` query** drive the gating (the `NavSidebar.test.tsx:7-8` discipline — never mock the hook).
  - `NavSidebar.test.tsx`: `shows Variables under Config` — link to `/custom-variables` rendered for a viewer session (not adminOnly).
  - `VariablePalette.test.tsx`: `fetched custom variables render in the Custom group` (catalog mock with one `tier:"custom"` entry) — if an equivalent assertion already exists, extend it to pin the PHI badge on a custom.
- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/CustomVariablesPage.test.tsx src/test/NavSidebar.test.tsx` — RED: module `features/customVariables/CustomVariablesPage` missing; nav link absent.
- [ ] Step 3: Implement — pattern-copy `features/adminUsers/AdminUsersPage.tsx`: table (name, description, example, PHI `Badge`), create/edit/delete dialogs (`ConfirmDialog`/`dialog` primitives), mutation buttons gated via `useIsAdmin` (the `AdminUsersPage.tsx:9` idiom); `hooks.ts`: `CustomVariable` interface, `useCustomVariables()` (key `["custom-variables"]`), `useCreateCustomVariable`/`useUpdateCustomVariable`/`useDeleteCustomVariable` — each `onSuccess` invalidates `["custom-variables"]` **and** `["variable-catalog"]`. Edit dialog has **no name input** (immutable; delete + recreate per spec §2). Route `{ path: "custom-variables", element: <CustomVariablesPage /> }` in `routes.tsx`; NavSidebar `Config` group gains `{ to: "/custom-variables", label: "Variables" }`. Update the `variableCatalog.ts:23-24` comment: the catalog is DB-backed now; CRUD mutations invalidate it. **Palette / `unknownTokens.ts` / `phiTokens.ts` get zero changes** — they consume the fetched catalog (spec §6.1).
- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/CustomVariablesPage.test.tsx src/test/NavSidebar.test.tsx src/test/VariablePalette.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui/src/features/customVariables apps/admin-ui/src/routes.tsx apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/config/variableCatalog.ts apps/admin-ui/src/test/CustomVariablesPage.test.tsx apps/admin-ui/src/test/NavSidebar.test.tsx apps/admin-ui/src/test/VariablePalette.test.tsx && git commit -m "feat(admin-ui): custom variables page — CRUD with catalog invalidation, Config nav entry"`

---

### Task C8: admin-ui — catalog-driven SMS notices in ToolsSection

**Files:**
- Modify: `apps/admin-ui/src/features/editor/sections/ToolsSection.tsx`
- Test: `apps/admin-ui/src/test/ToolsSection.test.tsx` (append)

- [ ] Step 1: Write the failing tests (mock `useVariableCatalog` with `diagnosis` (custom, phi) and `pet_name` (custom, non-phi)):
  - `sms body with phi custom shows blocked-at-save notice` — body `"{{diagnosis}}"` → a non-blocking notice stating the variable is PHI and **will be blocked at save** (server 422); the form remains submittable client-side (zod untouched).
  - `sms body with any custom shows renders-empty notice` — body `"{{pet_name}}"` → notice "custom variables are not substituted in SMS — renders empty" (mirror of the §3.2.1 server warning).
  - `builtin non-phi token shows neither notice` — body `"{{first_name}}"`.
- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/ToolsSection.test.tsx` — RED: notices not rendered.
- [ ] Step 3: Implement — inside the SMS templates editor in `ToolsSection.tsx`: `useVariableCatalog()`; per body, extract token names (reuse the `TOKEN_NAME_RE` pattern from `agentConfigSchema.ts` — export it or duplicate locally with a mirror comment), intersect with catalog `tier === "custom"` entries; render the two notices (the `phiTokens.ts` non-blocking-notice presentation pattern). **The static zod builtin-PHI superRefine and `PHI_TOKEN_NAMES` stay frozen on the 5 builtins** — add the comment: never drifts; server is authoritative for customs (spec §3.2.1 item 4).
- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/ToolsSection.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui/src/features/editor/sections/ToolsSection.tsx apps/admin-ui/src/test/ToolsSection.test.tsx && git commit -m "feat(admin-ui): catalog-driven SMS notices — custom-PHI blocked-at-save + renders-empty"`

---

### Task C9: admin-ui — `mapServerErrors` positional-slice fix + publish/rollback 422 routing

**Files:**
- Modify: `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx` (lines 94–108; **first of two sequential edits**: C9 → D11), `apps/admin-ui/src/features/editor/PublishDialog.tsx` (`handleConfirm`, lines 41–51), `apps/admin-ui/src/features/editor/hooks.ts` (`usePublish`, lines 48–58), `apps/admin-ui/src/features/versions/hooks.ts` (`useRollback` `onError`, line 31)
- Test: `apps/admin-ui/src/test/ProfileEditorPage.test.tsx` (append), `apps/admin-ui/src/test/VersionHistoryPage.test.tsx` (new)

- [ ] Step 1: Write the failing tests:
  - `ProfileEditorPage.test.tsx`: `save 422 with templates loc lands on the body field` — mock PUT draft returning 422 detail `[{"loc": ["body","config","tools","sms","templates",0,"body"], "msg": "…PHI…", "type": "value_error.custom_phi_sms"}]` → an error message renders on `tools.sms.templates.0.body` inside ToolsSection (today the value-filter eats the trailing `"body"` and maps to the row — RED); `publish 422 routes through mapServerErrors` — drive the publish flow through `PublishDialog` (the mutation fires in its `handleConfirm`, PublishDialog.tsx:41; today `usePublish`'s hook-level `onError: onApiError` at editor/hooks.ts:57 toasts the raw `err.detail`): mock publish 422 with the same shape → the field error renders **and exactly one** friendly toast appears (assert the raw-JSON toast is absent — the double-handler guard) — RED.
  - `VersionHistoryPage.test.tsx`: `rollback 422 surfaces the violation message` — mock rollback 422 with the same detail → the `msg` text appears in the toast (today `useRollback`'s hook-level `onError` at versions/hooks.ts:31 toasts raw `err.detail` — RED); never the raw JSON blob, never swallowed; exactly one toast.
- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/ProfileEditorPage.test.tsx src/test/VersionHistoryPage.test.tsx` — RED per the parentheticals.
- [ ] Step 3: Implement (**one error handler per mutation** — react-query v5 runs a per-`mutate` `onError` *in addition to* the hook-level one; adding a second handler produces a double toast, so handlers MOVE, never duplicate):
  - `mapServerErrors` (ProfileEditorPage.tsx:94-108): replace the value filter with the positional slice — assert `item.loc[0] === "body" && item.loc[1] === "config"`, then `item.loc.slice(2).join(".")` (comment: the custom-PHI loc ends in a field literally named `body` — filtering **by value** eats it, spec §6.3).
  - Publish: **drop** `usePublish`'s hook-level `onError: onApiError` (editor/hooks.ts:57); thread an `onPublishError(detail)` callback prop from `ProfileEditorPage` into `PublishDialog`, wired as the mutation's error path; the callback closes over the fixed `mapServerErrors` + toast fallback (same routing as save).
  - Rollback: replace the body of `useRollback`'s hook-level `onError` (versions/hooks.ts:31) with the parsed implementation — `tryParseFieldErrors` (export it from its current home if private) and toast each `msg`, falling back to `err.detail` for non-field errors; `VersionHistoryPage.tsx` adds **no** per-`mutate` handler.
- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/ProfileEditorPage.test.tsx src/test/VersionHistoryPage.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui/src/features/editor/ProfileEditorPage.tsx apps/admin-ui/src/features/editor/PublishDialog.tsx apps/admin-ui/src/features/editor/hooks.ts apps/admin-ui/src/features/versions/hooks.ts apps/admin-ui/src/test/ProfileEditorPage.test.tsx apps/admin-ui/src/test/VersionHistoryPage.test.tsx && git commit -m "fix(admin-ui): mapServerErrors positional loc slice + publish/rollback 422 routing (single-handler moves)"`

---

## Part D — Per-profile policy (Feature C)

### Task D1: `PolicyConfig` / `RetryMaxAttempts` schema + `AgentConfig.policy` + JSONB round-trip

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (third sequential edit)
- Test: `apps/api/tests/test_agent_config_schema.py` (append), `apps/api/tests/test_admin_profiles_custom_vars.py` (append the round-trip)

- [ ] Step 1: Write the failing tests:
  - `test_policy_config_narrowing_only` — parametrized `ValidationError`: start `"08:59"` (widens), end `"21:01"`, start `"12:00"` + end `"10:00"` (start ≥ end), start `"12:00"` + end `"12:00"`; valid: start `"09:30"` alone, end `"20:00"` alone, both `"10:15"`/`"18:45"` (minute granularity).
  - `test_policy_config_hhmm_format` — parametrized rejects: `"9:00"`, `"09:60"`, `"24:00"`, `"0900"`, `"09:00:00"`.
  - `test_policy_time_fields_stay_strings` — `PolicyConfig(quiet_hours_start_local="09:30").model_dump()["quiet_hours_start_local"] == "09:30"` (a `str`, never `datetime.time` — JSONB + zod round-trip contract, spec §3.3.1).
  - `test_retry_overrides_bounds` — `retry_delay_multiplier` 0.4/4.1 reject, 0.5/4.0 accept; each `RetryMaxAttempts` field rejects −1 and 5, accepts 0 and 4.
  - `test_agent_config_policy_optional_default_none` — extend `test_legacy_config_still_deserializes`: the prompts-only legacy dict validates and `cfg.policy is None` (forward-compat invariant, `agent_config.py:271-276`).
  - API round-trip (`test_admin_profiles_custom_vars.py`): `test_policy_jsonb_roundtrip_preserves_hhmm_strings` — PUT draft with `policy={"quiet_hours_start_local":"09:30","retry_delay_multiplier":2.0,"retry_max_attempts":{"busy":0}}` → 200; GET profile → `draft_config["policy"]["quiet_hours_start_local"] == "09:30"` exactly (not `"09:30:00"` — the `form.reset` contract, spec §3.3.1).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_agent_config_schema.py tests/test_admin_profiles_custom_vars.py -v` — RED: `ImportError: PolicyConfig` / unknown field accepted-then-dropped assertions fail.
- [ ] Step 3: Implement spec §3.3.1 verbatim: frozen `RetryMaxAttempts` (`no_answer/voicemail_left/busy/failed`, each `int | None`, `ge=0, le=4`, field comments stating the builtin equivalents **in the chain-global semantics**: builtin `no_answer: 2`, `voicemail_left/busy/failed: 1`) and frozen `PolicyConfig` (`quiet_hours_start_local/end_local: str | None` with `_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")` field validators + a private `_parse_hhmm(s) -> time`; `retry_delay_multiplier: float | None (ge=0.5, le=4.0)`; `retry_max_attempts: RetryMaxAttempts | None`); `model_validator(mode="after")` enforcing effective-start ≥ 09:00, effective-end ≤ 21:00, effective-start < effective-end (unset side = statutory). Comment block: times are **strings, full stop** — parsed only in validators and at consumption (`model_dump()` python-mode → JSONB would `TypeError` on `time`; `mode="json"` would round-trip `"09:30:00"` which the zod mirror rejects). `AgentConfig` gains `policy: PolicyConfig | None = None`.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_agent_config_schema.py tests/test_admin_profiles_custom_vars.py tests/test_admin_profiles_api.py tests/test_agent_config_resolve.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/agent_config.py apps/api/tests/test_agent_config_schema.py apps/api/tests/test_admin_profiles_custom_vars.py && git commit -m "feat(api): PolicyConfig — narrowing-only HH:MM quiet hours + bounded retry overrides, optional on AgentConfig"`

---

### Task D2: `quiet_hours.next_allowed` keyword generalization

**Files:**
- Modify: `apps/api/src/usan_api/quiet_hours.py`
- Test: `apps/api/tests/test_quiet_hours.py` (append)

- [ ] Step 1: Write the failing tests:
  - `test_next_allowed_narrowed_start_minute_granularity` — `start_local=time(9,30)`: 09:15 local → 09:30 local (UTC-converted); 09:30 exactly → unchanged.
  - `test_next_allowed_narrowed_end` — `end_local=time(17,0)`: 17:00 local → next morning at `start_local`; 16:59 → unchanged.
  - `test_next_allowed_narrowed_window_dst_spring_forward` — `America/New_York` on the spring-forward date with `start_local=time(10,30)`: pre-window instant clamps to 10:30 local with the **post-transition** offset (zoneinfo recompute pin).
  - `test_next_allowed_defaults_equal_statutory` — for a matrix of instants, `next_allowed(dt, tz) == next_allowed(dt, tz, start_local=time(9), end_local=time(21))` (zero-diff pin).
  - `test_unknown_timezone_still_raises_with_kwargs` — `ValueError` unchanged (fail-CLOSED).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_quiet_hours.py -v` — RED: `TypeError: unexpected keyword argument 'start_local'`.
- [ ] Step 3: Implement spec §3.3.2: `def next_allowed(dt_utc, tz_name, *, start_local: time = time(9, 0), end_local: time = time(21, 0)) -> datetime` — replace the hour comparisons with `time`-tuple comparisons (`local.time()` vs bounds) and build targets with `replace(hour=start_local.hour, minute=start_local.minute, …)`; keep `QUIET_START_HOUR`/`QUIET_END_HOUR` exported (statutory constants other modules import). Docstring: callers pass policy bounds already validated to be **within** statutory — this function does not re-clamp to statutory (the validator is the gate, spec §7).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_quiet_hours.py tests/test_retry_scheduling.py tests/test_dispatch_and_dial.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/quiet_hours.py apps/api/tests/test_quiet_hours.py && git commit -m "feat(api): next_allowed gains keyword-default start/end bounds (minute granularity, zero-diff defaults)"`

---

### Task D3: `retry_policy` — ladders as data, multiplier/max-attempts, `MAX_CHAIN_ATTEMPTS`

**Files:**
- Modify: `apps/api/src/usan_api/retry_policy.py`
- Test: `apps/api/tests/test_retry_policy.py` (append)

- [ ] Step 1: Write the failing tests:
  - `test_defaults_reproduce_v1_ladder_exactly` — parametrize the full existing matrix (NO_ANSWER 1→30m, 2→2h, 3→None; VOICEMAIL_LEFT 1→3h, 2→None; BUSY 1→5m, 2→None; FAILED 1→1m, 2→None; COMPLETED/DNC any→None) with **no kwargs** (zero-diff pin).
  - `test_multiplier_scales_every_rung` — `delay_multiplier=2.0`: NO_ANSWER 1→60m, 2→4h; `0.5`: BUSY 1→2.5m.
  - `test_max_attempts_zero_disables` — `max_attempts=0` → `None` at every attempt for every retryable status.
  - `test_max_attempts_extends_with_final_rung_repeat` — NO_ANSWER `max_attempts=4`: attempt 3 → 2h (index clamp to final rung), attempt 4 → 2h, attempt 5 → None; BUSY `max_attempts=3`: attempt 2 and 3 → 5m.
  - `test_mixed_status_chain_global_semantics` — the §3.3.1 pin: status changes across a chain key on the **chain-global** attempt number — BUSY at `attempt=3` with `max_attempts=3` retries (5m) even though attempts 1–2 were NO_ANSWER; with default (builtin 1) it returns None.
  - `test_max_chain_attempts_single_source` — `MAX_CHAIN_ATTEMPTS == 5` and `MAX_CHAIN_ATTEMPTS == 1 + 4` where 4 is the `RetryMaxAttempts` `le` bound (import the field metadata or pin the literal with a comment naming the coupling).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retry_policy.py -v` — RED: `TypeError` on kwargs; `ImportError: MAX_CHAIN_ATTEMPTS`.
- [ ] Step 3: Implement — `_LADDERS: dict[CallStatus, tuple[timedelta, ...]] = {NO_ANSWER: (30m, 2h), VOICEMAIL_LEFT: (3h,), BUSY: (5m,), FAILED: (1m,)}`; `MAX_CHAIN_ATTEMPTS = 5` with the spec §3.3.1 comment (root + 4 retries — the `le=4` ceiling; `_MAX_CHAIN_HOPS` and the `schedule_retry` walk bound DERIVE from this; raising `le=` without raising this reintroduces the chain-tip escape); `def next_retry_delay(status, attempt, *, max_attempts: int | None = None, delay_multiplier: float = 1.0) -> timedelta | None`: ladder = `_LADDERS.get(status)` (None → stop); `limit = max_attempts if max_attempts is not None else len(ladder)`; `attempt > limit` → None; else `ladder[min(attempt, len(ladder)) - 1] * delay_multiplier`.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retry_policy.py tests/test_retry_scheduling.py tests/test_retry_orchestrator.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/retry_policy.py apps/api/tests/test_retry_policy.py && git commit -m "feat(api): retry ladders as data — chain-global max_attempts + delay multiplier + MAX_CHAIN_ATTEMPTS single source"`

---

### Task D4: Chain-walk bounds derived from `MAX_CHAIN_ATTEMPTS`

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (second sequential edit: `_MAX_CHAIN_HOPS` line 610, `schedule_retry` root walk line 285)
- Test: `apps/api/tests/test_retry_scheduling.py` (append; **first of two sequential edits**: D4 → D6)

- [ ] Step 1: Write the failing tests:
  - `test_chain_hops_derive_from_policy_ceiling` — `calls_repo._MAX_CHAIN_HOPS == retry_policy.MAX_CHAIN_ATTEMPTS - 1` (today `3 != 4` → RED).
  - `test_get_chain_tip_reaches_depth_four_tip` — build root + 4 children directly via the repo/session (attempts 1..5, `parent_call_id` chained) → `get_chain_tip(db, root.id)` returns the depth-4 child (today the 3-hop bound stops one short → RED).
  - `test_batch_cancel_flips_max_depth_tip` — root with `idempotency_key="batch:<uuid>:0"` linked to a cancelled batch's target, chain to depth 4 with the tip QUEUED → `cancel_queued_tips(db, [root.id]) == 1` and the tip is CANCELLED (the escape the invariant exists to kill, spec §3.3.1).
  > The batch-root-walk suppression test (`test_schedule_retry_walk_finds_batch_root_at_max_depth`) lives in **D6**, not here: `schedule_retry` returns at the `next_retry_delay(...) is None` guard (`calls.py:266-268`) **before** the root walk (line 284), and with builtin ladders a depth-4 parent (attempt 4, NO_ANSWER) always hits that guard — only D6's policy threading (`no_answer=4`) makes the walk reachable at that depth. Writing it here would leave Step 4 RED.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v` — RED per the parentheticals (hop constant `3 != 4`; tip walk stops one short; cancel misses the depth-4 tip).
- [ ] Step 3: Implement — `from usan_api.retry_policy import MAX_CHAIN_ATTEMPTS, next_retry_delay`; `_MAX_CHAIN_HOPS = MAX_CHAIN_ATTEMPTS - 1` (replace the literal + rewrite the stale "ladders top out at 3 attempts" comment to cite the derivation); `schedule_retry`'s root walk `for _ in range(3)` → `for _ in range(_MAX_CHAIN_HOPS)`. Comment on the walk: this is a **derivation, not a bug fix** — under `le=4` the deepest parent that can still schedule a retry is attempt 4 = 3 hops from root, which `range(3)` already reached (spec §3.3.1 overstates this site); the genuinely load-bearing changes are the `get_chain_tip`/`cancel_queued_tips` bounds, and deriving the walk keeps all three from drifting if `le=`/`MAX_CHAIN_ATTEMPTS` ever rises.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retry_scheduling.py tests/test_batches_api.py tests/test_schedule_orchestrator.py tests/test_calls_lifecycle.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_retry_scheduling.py && git commit -m "fix(api): derive chain-walk bounds from MAX_CHAIN_ATTEMPTS — depth-4 tips cancellable, walk bound derived"`

---

### Task D5: `resolve_call_policy` + `ResolvedPolicy`

**Files:**
- Modify: `apps/api/src/usan_api/repositories/agent_profiles.py`
- Test: `apps/api/tests/test_resolve_call_policy.py` (new)

- [ ] Step 1: Write the failing tests (direct-session; seed profiles via repo create→draft→publish):
  - `test_statutory_defaults_when_nothing_resolves` — no profiles → `ResolvedPolicy(start_local=time(9), end_local=time(21), delay_multiplier=1.0)` and `max_attempts_for(<every retryable status>) is None`.
  - `test_policy_from_override_profile` — override profile published with policy (start "10:30", multiplier 2.0, busy 0) → parsed `time(10,30)`, `2.0`, `max_attempts_for(BUSY) == 0`.
  - `test_policy_from_elder_profile_when_no_override` — elder-assigned profile's policy used.
  - `test_whole_profile_precedence_override_without_policy_yields_statutory` — **the §3.3.2 pin:** override profile live but `policy=None`, elder profile narrows → result is **statutory** (whole-profile, never per-field merge; attaching a policy-less override loosens back to statutory — within the TCPA bound by construction).
  - `test_profile_with_policy_none_section_yields_statutory` — direction-default profile resolves, `policy` absent → statutory.
  - `test_invalid_snapshot_falls_through` — corrupt the published JSONB (raw UPDATE) → falls through (the `_resolved_from_profile` `ValidationError` path) → statutory.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_resolve_call_policy.py -v` — RED: `ImportError: resolve_call_policy`.
- [ ] Step 3: Implement in `agent_profiles.py` — frozen `@dataclass ResolvedPolicy(start_local: time, end_local: time, delay_multiplier: float, _max_attempts: Mapping[CallStatus, int | None])` with `max_attempts_for(status) -> int | None`; module-level `STATUTORY_POLICY` constant; `async def resolve_call_policy(db, *, profile_override, elder_profile_id, direction) -> ResolvedPolicy` — thin wrapper over `resolve_agent_config` (the same precedence walk: override → elder → direction default); `resolved is None or resolved.config.policy is None` → `STATUTORY_POLICY`; else parse the `"HH:MM"` strings to `time` and fill unset sides with statutory/None. Docstring carries the re-resolve-at-consumption decision (dial-time truth, spec §3.3.2; caching is Open Q8) and the whole-profile precedence consequence.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_resolve_call_policy.py tests/test_agent_config_resolve.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/agent_profiles.py apps/api/tests/test_resolve_call_policy.py && git commit -m "feat(api): resolve_call_policy — whole-profile precedence walk with statutory fallback"`

---

### Task D6: Wiring site 1 — `schedule_retry` (delay + clamp) + max-depth batch-root suppression pin

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (third sequential edit: lines 266 + 273)
- Test: `apps/api/tests/test_retry_scheduling.py` (append; second sequential edit: D4 → D6)

- [ ] Step 1: Write the failing tests (publish a policy profile, assign to the elder via `elder.agent_profile_id`):
  - `test_retry_delay_scaled_by_profile_multiplier` — NO_ANSWER parent attempt 1, multiplier 2.0 → child `scheduled_at ≈ now + 60min` (clamped).
  - `test_retry_suppressed_by_max_attempts_zero` — BUSY parent, policy `busy: 0` → `schedule_retry` returns `None`, no child.
  - `test_retry_clamped_to_narrowed_quiet_hours` — policy start `10:00`; FAILED parent at 09:20 local (1-min ladder) → child `scheduled_at` == 10:00 local in UTC.
  - `test_retry_honors_parent_override_policy_over_elder` — parent `profile_override` profile narrows, elder profile doesn't → override's policy applied (precedence threading pin).
  - `test_policy_reresolved_not_snapshotted` — create parent; **then** publish a tighter policy on the elder's profile; `schedule_retry` → child reflects the new policy (re-resolve at consumption, spec §3.3.2).
  - `test_schedule_retry_walk_finds_batch_root_at_max_depth` — **moved here from D4 (it needs this task's policy threading to be reachable):** elder profile policy `retry_max_attempts.no_answer = 4`; batch-rooted chain (root `idempotency_key="batch:<uuid>:0"` linked to a **cancelled** batch's target) down to a depth-4 parent (attempt 4) terminal NO_ANSWER — the policy would allow a 5th attempt, so `next_retry_delay` is non-None and the root walk runs → `schedule_retry` returns `None` with the `"Retry suppressed: batch cancelled"` log. Jointly pins D4's derived walk bound **and** D6's `max_attempts` threading.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v` — RED: children scheduled with builtin delays/statutory clamps; the max-depth suppression test gets a `None` *for the wrong reason* (the builtin-ladder `next_retry_delay` guard at `calls.py:266-268` fires before the walk — assert on the **log line**, which is absent today, not just the `None`).
- [ ] Step 3: Implement — inside `schedule_retry`, after the elder load: `policy = await agent_profiles_repo.resolve_call_policy(db, profile_override=parent.profile_override, elder_profile_id=elder.agent_profile_id, direction="outbound")` (import `agent_profiles` as a module — verified acyclic: `agent_profiles` does not import `calls`); `delay = next_retry_delay(parent.status, parent.attempt, max_attempts=policy.max_attempts_for(parent.status), delay_multiplier=policy.delay_multiplier)`; clamp via `quiet_hours.next_allowed(_utcnow() + delay, elder.timezone, start_local=policy.start_local, end_local=policy.end_local)`. **No signature change** (spec §3.3.2 site list). NOTE: the delay computation moves **after** the elder load (the policy resolve needs `elder.agent_profile_id`) — preserve the early-return order: parent/elder missing → None, then delay-None → None, then tz clamp, then batch-root walk.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retry_scheduling.py tests/test_calls_lifecycle.py tests/test_tools.py tests/test_webhooks.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_retry_scheduling.py && git commit -m "feat(api): schedule_retry resolves per-profile policy — scaled/truncated ladders + narrowed quiet-hours clamp + max-depth batch-root pin"`

---

### Task D7: Wiring site 2 — `dispatch_and_dial` dial-moment re-check

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py` (the quiet-hours re-check at line ~360)
- Test: `apps/api/tests/test_dispatch_and_dial.py` (append)

- [ ] Step 1: Write the failing tests — reuse the module's existing helpers by their real names: `_seed_dialing_retry(factory, *, room, tz=…)` (seeder) and `_pin_inside_quiet_hours(monkeypatch)` (which monkeypatches `livekit_dispatch._utcnow`). The seeded elder has **no** `agent_profile_id` — extend the seeder (new kwarg or a sibling helper) to create→publish a policy profile and assign it to the elder; add a frozen-time pin variant for the policy scenarios:
  - `test_dial_requeued_under_narrowed_policy_window` — elder profile policy start `10:00`; frozen now = 09:30 local → `requeue_for_quiet_hours` path taken: row back to QUEUED with `scheduled_at` == 10:00 local in UTC; `DIAL_REQUEUED_TOTAL{reason="quiet_hours"}` incremented; no dial attempted (this is what makes a tightened window effective for already-queued calls, spec §3.3.2).
  - `test_dial_proceeds_inside_policy_window` — now = 10:30 local → dial proceeds (no requeue).
  - `test_dial_requeue_honors_call_override_policy` — `call.profile_override` profile narrows; elder profile statutory → override wins.
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v` — RED: 09:30 is inside statutory hours → dial proceeds.
- [ ] Step 3: Implement — in `dispatch_and_dial`'s re-check block (the `quiet_hours.next_allowed` call at `livekit_dispatch.py:360`): resolve `policy = await agent_profiles_repo.resolve_call_policy(db, profile_override=call.profile_override, elder_profile_id=elder.agent_profile_id, direction="outbound")` and pass `start_local=policy.start_local, end_local=policy.end_local` to the existing `next_allowed` call. Extend the standing comment: **ad-hoc immediate dials (`_dial_and_classify`, line 245 — the worker behind the public `dial_and_classify` at 225) bypass this entirely** — pre-existing statutory gap, §2 non-goal / Open Q5; policy first binds an ad-hoc call's retries.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py tests/test_livekit_dispatch.py tests/test_retry_orchestrator.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/livekit_dispatch.py apps/api/tests/test_dispatch_and_dial.py && git commit -m "feat(api): dial-moment quiet-hours re-check is policy-aware (poller dials re-queue under narrowed windows)"`

---

### Task D8: Wiring sites 3+4 — `schedule_windows` policy composition + both orchestrator clamps

**Files:**
- Modify: `apps/api/src/usan_api/schedule_windows.py` (`effective_window`, `next_run_at`), `apps/api/src/usan_api/schedule_orchestrator.py` (occurrence branch lines ~290–351, `_materialize_one_target` lines ~470–515), `apps/api/src/usan_api/routers/schedules.py` (`_compute_next_run_at` defensive branch only), `apps/api/src/usan_api/observability/custom_metrics.py` (module docstring only), `apps/api/migrations/versions/0012_batch_scheduled_calling.py` (stale `skip_reason` comment vocabulary at line 113, comment-only — no schema change)
- Test: `apps/api/tests/test_schedule_windows.py` (append), `apps/api/tests/test_schedule_orchestrator.py` (append), `apps/api/tests/test_batch_observability.py` (comment refresh in the docstring-pin test, lines ~107–113)

- [ ] Step 1: Write the failing tests:
  - `test_schedule_windows.py`:
    - `test_effective_window_intersects_policy` — window 09:00–12:00, policy 11:00–21:00 → `(time(11), time(12))`; policy-free call unchanged (zero-diff).
    - `test_effective_window_policy_empty_returns_none` — window 09:00–10:00, policy start 11:00 → `None`.
    - `test_next_run_at_policy_push_lands_at_policy_start` — window 09:00–12:00, policy start 11:00, `after` = 08:00 local → 11:00 local (never inside the policy-forbidden 09:00–11:00 zone — the §3.3.3 composition bug pin).
    - `test_next_run_at_policy_empty_returns_none` — window 09:00–10:00, policy start 11:00 → `None` (no raise).
    - `test_next_run_at_statutory_empty_still_raises` — window 21:00–22:00, no policy kwargs → `ValueError` (the reserved contract).
  - `test_schedule_orchestrator.py`:
    - `test_batch_target_policy_window_empty_skips_observably` — batch window 09:00–10:00; elder profile policy start 11:00 → target `mark_target_skipped(reason="window")`, `_materialize_one_target` returns `"skipped_window"`, **no call row** (never scheduled outside the window, never silently dropped — §3.3.3 rule 2).
    - `test_batch_target_dial_pushed_to_policy_start_inside_window` — window 09:00–18:00, policy start 11:00, now 09:30 local → materialized call `scheduled_at` == 11:00 local.
    - `test_schedule_occurrence_policy_window_empty_skips` — schedule window 09:00–10:00, policy start 11:00, now 09:15 (a weekday inside the days-mask) → result `skipped_window`, **no call row**, `next_run_at` bookkeeping advanced policy-free (§3.3.3 rule 2 on the occurrence path).
    - `test_schedule_occurrence_policy_end_clamp_past_skips` — schedule window 09:00–12:00, policy **end** 10:00, now 10:30 → result `skipped_window` (the statutory window is still open at 10:30 — pins that the skip keys on the **effective** end, not the statutory one; clamp-before-skip, §3.3.3 rule 3); companion case now 09:45 → `created` with `scheduled_at` == 09:45; `next_run_at` advanced policy-free in both.
    - `test_schedule_occurrence_policy_push_no_reschedule_loop` — **the re-claim-loop pin (executor note 5):** schedule window 09:00–12:00, policy start 09:30, claimed at exactly 09:00 (its statutory `next_run_at`) → exactly one `created` with `scheduled_at` == 09:30 local, and the recorded result is **not** `rescheduled` (a naive policy-narrowed `start_utc` would take the `schedule_orchestrator.py:307` staleness branch, record `rescheduled` with `next_run_at(now) == now`, and re-claim every cycle until 09:30).
    - `test_policy_free_profiles_orchestrate_unchanged` — no policy anywhere → one existing happy-path scenario reproduced byte-for-byte (ship-inert pin).
- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedule_windows.py tests/test_schedule_orchestrator.py -v` — RED: `TypeError` on the new kwargs; batch target dials at 09:30; occurrence dials inside the policy-forbidden zone / past the policy end.
- [ ] Step 3: Implement:
  - `schedule_windows.effective_window(start, end, *, policy_start: time | None = None, policy_end: time | None = None)` — intersect with `max(_QUIET_START, policy_start or _QUIET_START)` / `min(_QUIET_END, policy_end or _QUIET_END)`; `next_run_at(..., policy_start=None, policy_end=None) -> datetime | None` — `None` **only** when the policy-narrowed window is empty while the statutory window is non-empty (executor note 5); module docstring updated.
  - `routers/schedules.py` `_compute_next_run_at` + the orchestrator's two policy-free `next_run_at` call sites: defensive `if computed is None: raise ValueError("unreachable: policy bounds not passed")`.
  - Orchestrator occurrence branch — **two-window rule (executor note 5):** the existing statutory computation (lines 297–305) and the `now < start_utc` staleness branch (line 307) stay **byte-identical** (statutory window + days-mask only — keying staleness on a policy-narrowed start re-claims the row every cycle). *After* the staleness branch: resolve `policy = await agent_profiles_repo.resolve_call_policy(db, profile_override=schedule.profile_override, elder_profile_id=elder.agent_profile_id, direction="outbound")` once; compute `eff = effective_window(schedule.window_start_local, schedule.window_end_local, policy_start=policy.start_local, policy_end=policy.end_local)` — `None` → `record_result(result="skipped_window", next_run_at=<policy-free next_occurrence>)`; else `eff_start_utc/eff_end_utc` from the effective bounds, **clamp-before-skip**: `dial_at = max(now, eff_start_utc)`; `dial_at >= eff_end_utc` → `skipped_window`; else materialize with `scheduled_at=dial_at` (replaces the bare `quiet_hours.next_allowed(now, …)` at line 347). The pre-existing statutory `now >= end_utc` late-poller skip (line 334) stays untouched.
  - `_materialize_one_target`: `dial_at = quiet_hours.next_allowed(now, elder.timezone, start_local=policy.start_local, end_local=policy.end_local)` (policy resolved with target-over-batch override precedence — reuse the existing `profile_override` computation, hoisted above the dial-at math); windowed batches call `next_run_at(..., policy_start=…, policy_end=…)`; `None` → `mark_target_skipped(reason="window")` + return `"skipped_window"` (new bounded `MATERIALIZED_CALLS_TOTAL` result value; skips log at WARNING, ids only).
  - **Metrics-invariant bookkeeping:** `observability/custom_metrics.py` module docstring currently declares `result="skipped_window"`/`"rescheduled"` with `source="batch"` "structurally impossible — never emitted" (line ~19) — amend: `batch×skipped_window` is now emitted (policy∩window=∅, this task); `batch×rescheduled` remains impossible. Refresh the citing comment in `tests/test_batch_observability.py` (~107–113; its `in doc` assertions still pass). Update the stale `0012` migration `skip_reason` comment vocabulary (`elder_deleted | invalid_timezone | key_conflict | daily_cap` → append `window`; comment-only).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedule_windows.py tests/test_schedule_orchestrator.py tests/test_schedules_api.py tests/test_batches_api.py tests/test_materializer.py tests/test_schedule_schemas.py tests/test_batch_observability.py -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/schedule_windows.py apps/api/src/usan_api/schedule_orchestrator.py apps/api/src/usan_api/routers/schedules.py apps/api/src/usan_api/observability/custom_metrics.py apps/api/migrations/versions/0012_batch_scheduled_calling.py apps/api/tests/test_schedule_windows.py apps/api/tests/test_schedule_orchestrator.py apps/api/tests/test_batch_observability.py && git commit -m "feat(api): policy-aware window composition — statutory staleness check, single-place intersection, skip-observably on empty, clamp-before-skip"`

---

### Task D9: Agent-side regression pin — `policy` key ignored

**Files:**
- Test only: `services/agent/tests/test_agent_config.py` (append)

- [ ] Step 1: Write the test — `test_unknown_policy_key_is_ignored`: a runtime-config payload dict (valid prompts + a `"policy": {"quiet_hours_start_local": "10:00"}` key) → `AgentConfig.model_validate(payload)` succeeds and the instance has no `policy` attribute. Comment: pins pydantic's default `extra="ignore"` on the agent mirror (`agent_config.py:95` sets only `frozen=True`) — a future `extra="forbid"` cleanup would break runtime config fetch for every policy-carrying profile (spec §3.3.1).
- [ ] Step 2: `cd services/agent && uv run pytest tests/test_agent_config.py -v` — this is a regression PIN and should be GREEN immediately; if it is RED, the agent mirror already forbids extras and the spec's rollout assumption is broken — **stop and escalate**, do not change agent `src`.
- [ ] Step 3: No implementation (agent untouched).
- [ ] Step 4: `cd services/agent && uv run pytest -v && uv run ruff check . && uv run mypy`
- [ ] Step 5: `git add services/agent/tests/test_agent_config.py && git commit -m "test(agent): pin extra='ignore' — policy key in runtime config must never break the agent"`

---

### Task D10: admin-ui — `policySchema` zod mirror + `fieldMeta` policy section

**Files:**
- Modify: `apps/admin-ui/src/config/agentConfigSchema.ts`, `apps/admin-ui/src/config/fieldMeta.ts`, `apps/admin-ui/src/types/api.ts` (AgentConfig type gains `policy?`)
- Test: `apps/admin-ui/src/test/agentConfigSchema.test.ts` (append), `apps/admin-ui/src/test/fieldMeta.test.ts` (append)

- [ ] Step 1: Write the failing tests:
  - `agentConfigSchema.test.ts`: `policySchema mirrors pydantic bounds` — rejects start `"08:59"`, end `"21:01"`, start `"12:00"`+end `"10:00"` (superRefine issue with `path: ["quiet_hours_start_local"]`), `"9:00"` (regex `/^([01]\d|2[0-3]):[0-5]\d$/`), multiplier 0.4/4.1, `retry_max_attempts.busy` 5/−1; accepts one-sided `"09:30"`, minute granularity; `empty time input transforms to null` — `policySchema.parse({quiet_hours_start_local: "", …})` → `null` (cleared `<input type="time">` yields `""`, not `null` — pristine forms must validate, spec §6.2); `older draft without policy resets cleanly` — `agentConfigSchema.parse(<draft lacking the key>)` passes (`.optional().nullable()`, shaped like `toolsSchema.sms`); `policy object paths match pydantic field names` (the 422-loc mapping contract).
  - `fieldMeta.test.ts`: `"policy"` present in `SECTION_LABELS` (label `"Policy"`); `fieldMeta` carries entries for `policy.quiet_hours_start_local`, `policy.quiet_hours_end_local`, `policy.retry_delay_multiplier`, and the four `policy.retry_max_attempts.*` keys; the `retry_max_attempts` help text mentions chain-global attempts and final-rung repeat (Open Q2 disposition).
- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/agentConfigSchema.test.ts src/test/fieldMeta.test.ts` — RED: `policySchema` not exported; `policy` missing from labels.
- [ ] Step 3: Implement — `export const policySchema` per spec §6.2: HH:MM regex with `z.string().regex(...).nullable()` preceded by an empty-string→null `z.preprocess`/`transform`; numeric bounds 1:1 with `PolicyConfig`; narrowing + start<end via `.superRefine` with `path`; `agentConfigSchema` gains `policy: policySchema.optional().nullable()`; `types/api.ts` `AgentConfig` interface gains the optional `policy` shape.
- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/agentConfigSchema.test.ts src/test/fieldMeta.test.ts && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui/src/config/agentConfigSchema.ts apps/admin-ui/src/config/fieldMeta.ts apps/admin-ui/src/types/api.ts apps/admin-ui/src/test/agentConfigSchema.test.ts apps/admin-ui/src/test/fieldMeta.test.ts && git commit -m "feat(admin-ui): policySchema zod mirror (HH:MM, narrowing superRefine, empty-string-to-null) + policy fieldMeta"`

---

### Task D11: admin-ui — `PolicySection` + editor integration

**Files:**
- Create: `apps/admin-ui/src/features/editor/sections/PolicySection.tsx`
- Modify: `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx` (second sequential edit: `SECTION_ORDER` line ~25 + conditional render block lines ~193–200)
- Test: `apps/admin-ui/src/test/PolicySection.test.tsx` (new), `apps/admin-ui/src/test/ProfileEditorPage.test.tsx` (append)

- [ ] Step 1: Write the failing tests:
  - `PolicySection.test.tsx` (modeled on the `TimingSection`/`VoicemailSection` tests): `renders two time inputs, multiplier, and four attempt inputs with effective-default placeholders` — placeholders `09:00`, `21:00`, `×1.0` (or `1.0`), and the builtin attempt defaults shown as **placeholders, never values** (unset state, spec §6.2); `widening start shows validation error` — type `08:00` → the superRefine message renders; `clearing a time input keeps the form valid` (the `""`→null transform end-to-end).
  - `ProfileEditorPage.test.tsx`: `Policy section renders in order` — section heading "Policy" appears after the existing sections; `older draft without policy loads without errors` (form.reset acceptance at the page level).
- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/PolicySection.test.tsx src/test/ProfileEditorPage.test.tsx` — RED: module missing; section absent.
- [ ] Step 3: Implement — `PolicySection.tsx` on the `TimingSection.tsx` pattern (`Field` + `controls.tsx` inputs): two `<input type="time">`, one number input (multiplier, step 0.1), four bounded number inputs (0–4); help text from `fieldMeta`; placeholders per spec §6.2. `ProfileEditorPage.tsx`: append `"policy"` to `SECTION_ORDER`, add the conditional render line; optional rail summary via targeted `form.watch` (skip if it bloats — record the choice).
- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/PolicySection.test.tsx src/test/ProfileEditorPage.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui/src/features/editor/sections/PolicySection.tsx apps/admin-ui/src/features/editor/ProfileEditorPage.tsx apps/admin-ui/src/test/PolicySection.test.tsx apps/admin-ui/src/test/ProfileEditorPage.test.tsx && git commit -m "feat(admin-ui): Policy editor section — quiet-hours narrowing + retry overrides with effective-default placeholders"`

---

## Part E — Full-suite gate

### Task E1: Gate

- [ ] Step 1: Update docs — admin-ui README variable section: custom tier, the SMS renders-empty caveat, the Policy section (spec §9). `git add` + fold into the Step 4 commit.
- [ ] Step 2: Run, in order, every line **from the repo root**:

```bash
cd apps/api && uv run pytest -v --tb=short && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd services/agent && uv run pytest -v && uv run ruff check . && uv run mypy   # src untouched but must stay green
cd apps/admin-ui && npm run typecheck && npm run lint && npm run test
# Agent-src-untouched check (D9 adds a TEST only; src must show nothing):
git diff --name-only b7d2b8c...HEAD -- services/agent/src    # MUST print nothing
# (after the executor-note-2 rebase onto origin/main, use: git diff --name-only origin/main...HEAD -- services/agent/src)
cd apps/api && uv run --with pytest-cov pytest tests/ \
  --cov=usan_api.repositories.custom_variables --cov=usan_api.schemas.custom_variables \
  --cov=usan_api.routers.admin_custom_variables --cov=usan_api.routers.admin_variable_catalog \
  --cov=usan_api.schemas.agent_config --cov=usan_api.routers.admin_profiles \
  --cov=usan_api.sms_render --cov=usan_api.schemas.call --cov=usan_api.routers.calls \
  --cov=usan_api.repositories.calls --cov=usan_api.repositories.agent_profiles \
  --cov=usan_api.quiet_hours --cov=usan_api.retry_policy --cov=usan_api.schedule_windows \
  --cov=usan_api.schedule_orchestrator --cov=usan_api.livekit_dispatch \
  --cov-report=term-missing --cov-fail-under=80
```
  The coverage command is a **hard gate**: no `|| true`; scoped to every changed module — `repositories/calls.py` and `schedule_orchestrator.py` carry the highest-risk diffs (retry policy threading, window composition) and must not hide outside it.

- [ ] Step 3: Walk the **spec §8 conformance checklist** — every row ticks against a named passing test:
  - [ ] §8 Feature A: schema optional/UUID → `test_create_call_request_profile_override_optional_uuid`; 422 non-live (draft/archived/missing) → `test_enqueue_422_when_override_not_live`; **ordering: enqueue → archive → replay 200** → `test_replay_identical_after_override_archived_returns_200`; replay 200 identical / 409 differing (None→set, set→set, both consumers) → `test_replay_with_different_override_409` + `test_idempotent_replay_helper_409_on_override_mismatch`; DNC persistence → `test_dnc_path_persists_override`; kwarg threading → `test_create_call_persists_profile_override`; echo → `test_call_response_echoes_profile_override`; runtime e2e → `test_runtime_config_resolves_adhoc_override_end_to_end`.
  - [ ] §8 Feature B: CRUD mirror (401/403/404/409/422 slug/collision/name-change) → `test_admin_custom_variables_api.py`; audit incl. phi old/new → `test_mutations_audited_with_phi_old_new_detail`; catalog merge order/tier/default + **shadowed-dropped-and-logged** → `test_variable_catalog_api.py` additions; declared-custom-not-unknown / undeclared-warns / renders-empty / 422 with exact loc / sensitive-field warning → `test_admin_profiles_custom_vars.py`; **rollback 422** → `test_rollback_422_when_snapshot_references_now_phi_custom`; **send-time invariant** → `test_custom_tokens_render_empty_even_with_value_in_dynamic_vars`.
  - [ ] §8 Feature C: pure functions (narrowed windows, minute granularity, DST, defaults unchanged; multiplier, truncation, extension, **mixed-status chains**, exact v1 ladder) → `test_quiet_hours.py` + `test_retry_policy.py` additions; **chain-bound invariant + max-depth batch cancel** → D4 tests; **batch-root-walk suppression at max depth** → D6's `test_schedule_retry_walk_finds_batch_root_at_max_depth` (moved from D4 — unreachable without policy threading); PolicyConfig validators → D1 tests; **JSONB HH:MM round-trip** → `test_policy_jsonb_roundtrip_preserves_hhmm_strings`; forward-compat → extended `test_legacy_config_still_deserializes`; precedence + **whole-profile pin** → `test_resolve_call_policy.py`; four wiring sites → D6 (schedule_retry), D7 (dial re-queue), D8 (both orchestrator clamps); **§3.3.3 composition matrix** (∅→skip batch + occurrence, policy-end clamp-past→skip, push-never-in-forbidden-zone, **no reschedule loop**) → D8 tests; **agent `extra="ignore"` pin** → `test_unknown_policy_key_is_ignored`.
  - [ ] §8 admin-ui: policySchema mirror/superRefine/transform/older-draft reset → D10; PolicySection render/placeholders/errors → D11; custom-variables CRUD flows → C7; palette pickup → C7; both SMS notices → C8; **mapServerErrors positional slice → `tools.sms.templates.0.body`** + publish/rollback 422 routing (single-handler moves) → C9.
  - [ ] ruff + mypy + tsc + eslint + vitest → the Step 2 commands.
- [ ] Step 4: Commit anything outstanding (explicit `git add`, e.g. `docs(docs): admin-ui README — custom variables tier, SMS caveat, policy section`), then **stop**. Do not open the PR against `main` while #55/#56/#57 are open — A4's `down_revision="0014"` hard-couples its merge to #57 (executor note 2); single squash PR per the established plan-PR workflow.

---

## Planner disposition (deviations & their grounds)

- **Part ordering follows the orchestrator's A–E breakdown** (migration → profile_override → custom variables → policy → gate), not spec §9's ship order (which put Feature B before Feature A). No dependency crosses the boundary: Feature A touches only `calls`-plane files; Feature B's only Part-A dependency is the 0015 table.
- **`next_run_at -> datetime | None`** with `None` reserved for policy-induced emptiness (executor note 5) — the spec demanded "returns None for the policy-induced empty case rather than raising"; the Optional return + defensive raises at the three policy-free call sites is the smallest mypy-strict-clean realization.
- **Cadence stays policy-free**: `next_occurrence`/`record_result` bookkeeping never sees policy bounds — policy narrows *when an occurrence dials or skips*, never *when the schedule recurs*. Pinned by the D8 occurrence tests advancing `next_run_at`.
- **Occurrence staleness check stays statutory (two-window rule)** — spec §3.3.3 did not address the interplay between the policy-narrowed window and the pre-existing `now < start_utc → rescheduled` branch (`schedule_orchestrator.py:307`); keying it on policy bounds re-claims the row every poll cycle until policy start (violating the lines-284-288 invariant and inflating `rescheduled`). The staleness check keys on statutory bounds; the policy push happens at `dial_at = max(now, eff_start_utc)`. Pinned by `test_schedule_occurrence_policy_push_no_reschedule_loop`.
- **Spec §3.3.1's root-walk claim corrected in place** — "the root walk would never reach the `batch:` root" overstates: under `le=4` the deepest retry-scheduling parent is attempt 4 = 3 hops, which `range(3)` already reaches. The walk change is kept as a drift-proof derivation; the load-bearing fixes are the `get_chain_tip`/`cancel_queued_tips` bounds (D4). The max-depth suppression test lives in D6 where policy threading makes the walk reachable.
- **`custom_phi_sms_violations` lives in `schemas/agent_config.py`** beside `_reject_phi_in_templates` and `_TOKEN_RE` (one tokenizer, one PHI-message shape); the routers own only the phi-name fetch and the `HTTPException(422, detail=violations)` raise.
- **Batch skip strings pinned** (`reason="window"`, result `"skipped_window"`) — the spec named the outcome but not the target-row reason vocabulary; `"skipped_window"` joins the existing bounded `MATERIALIZED_CALLS_TOTAL` result label set, and the `custom_metrics.py` "structurally impossible" docstring + its `test_batch_observability.py` pin + the `0012` comment vocabulary are updated in the same task (D8) so documentation never contradicts emission.
- **Rollback 422 → toast via `useRollback`'s hook-level `onError`** (no form to map onto); save+publish map onto form fields via the fixed `mapServerErrors`, with publish routed through an `onPublishError` prop into `PublishDialog`. Error handlers **move** — never duplicated — because react-query v5 runs per-`mutate` and hook-level `onError` both. The spec's intent — field-loc'd custom-PHI errors are never swallowed on any of the three mutation paths — is pinned per-surface.
- **D9 is a green-on-arrival regression pin**, not RED-first: it pins existing load-bearing behavior (`extra="ignore"`); a RED result is an escalation signal, not an implementation cue.
- **`description ≤ 500` / `example ≤ 200` caps** on `CustomVariableCreate` — the spec left pydantic caps unpinned; bounded to match the house pattern (DB stays TEXT).

## Review findings disposition (plan review applied 2026-06-10)

All review findings were verified against the tree at `1b05a31` and **accepted — none rejected**:

- **HIGH (D4 broken RED→GREEN chain)** — applied: the batch-root-walk suppression test moved D4 → D6 (the `next_retry_delay` None-guard at `calls.py:266-268` precedes the walk; only D6's `no_answer=4` threading makes attempt-4 parents reach it); D4 keeps the three derivation/tip/cancel tests; the spec's overstated root-walk claim is corrected in the D4 implementation comment.
- **HIGH (C9 unimplementable in listed files)** — applied: `PublishDialog.tsx`, `editor/hooks.ts`, `versions/hooks.ts` added to C9; `usePublish`'s hook-level `onError` dropped in favor of an `onPublishError` prop; `useRollback`'s hook-level `onError` replaced in place; single-handler rule recorded (react-query v5 double-toast hazard).
- **HIGH (D8 staleness-branch interplay unspecified)** — applied: two-window rule specified in executor note 5 + D8 Step 3 (statutory staleness check, policy push via `dial_at`); `test_schedule_occurrence_policy_push_no_reschedule_loop` added.
- **MEDIUM (D8 mislabeled clamp test)** — applied with one adaptation: the reviewer's suggested re-scenario (window 09:00–10:00, policy 09:30–21:00, now 10:05) would fire the pre-existing **statutory** late-poller skip (line 334) before any policy code runs; the genuine rule-3 case narrows the policy **end** instead (window 09:00–12:00, policy end 10:00, now 10:30) — `test_schedule_occurrence_policy_end_clamp_past_skips`. The rule-2 occurrence coverage the mislabeled test accidentally provided is preserved as `test_schedule_occurrence_policy_window_empty_skips`.
- **MEDIUM (batch×skipped_window metrics invariant)** — applied: `custom_metrics.py` docstring, `test_batch_observability.py` comment, and `0012` `skip_reason` comment vocabulary added to D8's file list (all comment/docstring-only).
- **MEDIUM (C7 test-idiom mismatch)** — applied: msw references removed; route-by-URL `vi.mock("../lib/api")` fake; viewer gating via mocked `/v1/auth/me` + real `useIsAdmin` (never hook-mocked); also recorded in executor note 7.
- **LOW (D7 helper drift)** — applied: `_seed_dialing_retry` + `_pin_inside_quiet_hours` named; seeder extension for `agent_profile_id` called out; `_dial_and_classify`:245 / `dial_and_classify`:225 corrected (incl. files-read appendix).
- **LOW (executor note 3 incomplete)** — applied: test-file sequencing added (`test_agent_config_schema.py` C5→C6→D1, `test_admin_profiles_custom_vars.py` C5→C6→D1, `test_retry_scheduling.py` D4→D6) and mirrored in the D4/D6 file lists.

## Files read (for reference)
- Spec: `docs/superpowers/specs/2026-06-10-small-unlocks-design.md` (full); format reference: `docs/superpowers/plans/2026-06-10-plan-outbound-webhooks.md` (full)
- `apps/api/src/usan_api/`: `schemas/call.py`, `routers/calls.py` (replay helper 35–42, pre-check 123, fallback 70), `repositories/calls.py` (create_call 56, schedule_retry 254–320 incl. delay guard 266–268, root walk 285, `_MAX_CHAIN_HOPS` 610, cancel_queued_tips 638), `repositories/agent_profiles.py` (resolve walk 302–323, `_resolved_from_profile` 271–299, `is_live_profile` 326), `routers/schedules.py` (`_require_live_override` 69–75, `_compute_next_run_at`), `routers/batches.py` (`_OVERRIDE_ERROR` 49), `routers/admin_profiles.py` (save 92–137, publish 140–165, rollback 188–213), `routers/admin_users.py` (CRUD precedent, full), `routers/admin_variable_catalog.py` (full), `routers/runtime.py` (full), `routers/tools.py` (schedule_callback 143–166, send_sms resolve 244), `schemas/agent_config.py` (full), `schemas/variable_catalog.py` (full), `sms_render.py` (full), `quiet_hours.py` (full), `retry_policy.py` (full), `schedule_windows.py` (full), `schedule_orchestrator.py` (invariant comment 284–288, occurrence branch 290–351 incl. staleness branch 307, late-poller skip 334, dial clamp 347, `_materialize_one_target` 460–530), `livekit_dispatch.py` (dial-moment re-check ~353–390 incl. `next_allowed` at 360, `dial_and_classify` 225, `_dial_and_classify` 245), `observability/custom_metrics.py` (impossible-combo docstring ~19, label comment 96–99), `db/models.py` (model inventory), `main.py` (router registration 155–173), `migrations/versions/0014_outbound_webhooks.py` (header style), `migrations/versions/0012_batch_scheduled_calling.py` (`skip_reason` comment 113)
- `apps/api/tests/`: `conftest.py` (TRUNCATE 104–110, `client`, `admin_session` 218, `operator_headers`), `test_ops_queue_migration.py` (helper inventory), `test_admin_users_api.py`, `test_variable_catalog_api.py`, `test_agent_config_schema.py` (`test_legacy_config_still_deserializes` 38), `test_batch_observability.py` (docstring pin ~107–113), `test_dispatch_and_dial.py` (`_pin_inside_quiet_hours` 51, `_seed_dialing_retry` 59)
- `services/agent/src/usan_agent/agent_config.py` (AgentConfig mirror, `frozen=True` only ~95); `services/agent/tests/` inventory
- `apps/admin-ui/src/`: `config/agentConfigSchema.ts` (full), `config/variableCatalog.ts` (full), `config/fieldMeta.ts` (SectionKey/SECTION_LABELS 11–31), `features/editor/ProfileEditorPage.tsx` (mapServerErrors 94–108, SECTION_ORDER 25, render block 193–200), `features/editor/PublishDialog.tsx` (handleConfirm 41–51), `features/editor/hooks.ts` (usePublish 48–58 incl. onError 57), `features/editor/sections/ToolsSection.tsx` (catalog usage), `features/versions/hooks.ts` (useRollback onError 31), `features/versions/VersionHistoryPage.tsx`, `components/NavSidebar.tsx` (GROUPS 17–40), `routes.tsx`, test inventory (`NavSidebar.test.tsx` 7–11 real-useIsAdmin discipline), `package.json` scripts
