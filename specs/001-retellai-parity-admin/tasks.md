---
description: "Task list for RetellAI-Parity Admin Console & Agent Studio"
---

# Tasks: RetellAI-Parity Admin Console & Agent Studio

**Input**: Design documents from `specs/001-retellai-parity-admin/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included — Constitution IV (Test-First, NON-NEGOTIABLE) + the 80%-coverage rule make TDD
mandatory for this repo. Within each story, write the listed tests FIRST and confirm they FAIL before
implementing.

**Organization**: Tasks are grouped by user story (from spec.md priorities) so each story is an
independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5 (user-story phases only); Setup/Foundational/Polish carry no story label
- Every task names an exact file path

## Path Conventions

`apps/api/src/usan_api/…` (FastAPI), `apps/api/tests/…` (pytest), `apps/api/migrations/versions/…`
(Alembic); `services/agent/src/usan_agent/…` + `services/agent/tests/…`; `apps/admin-ui/src/…` +
`apps/admin-ui/src/test/…` (Vitest).

---

## Phase 1: Setup (shared scaffolding)

**Purpose**: Dependencies and validated configuration that later phases rely on. No behavior change.

- [ ] T001 [P] Add `google-genai` (Vertex AI / `vertexai=True`) dependency for the text-test LLM path in `apps/api/pyproject.toml`, then `cd apps/api && uv sync`
- [ ] T002 [P] Add `livekit-client` (^2.x) dependency for the audio-test webcall in `apps/admin-ui/package.json`, then `cd apps/admin-ui && npm ci`
- [X] T003 Add startup-validated settings fields in `apps/api/src/usan_api/settings.py`: `cartesia_api_key: SecretStr`, `cartesia_api_url`, `cartesia_version`, `cartesia_sample_model`, `gcp_project`, `vertex_location` (Pydantic, fail-fast at startup per Constitution III)
- [X] T004 [P] Add the new keys to `infra/.env.example` and document the "VM `.env` before the deploy tag" ordering for `CARTESIA_API_KEY` / `GCP_PROJECT` / `VERTEX_LOCATION` in `infra/README.md`

---

## Phase 2: Foundational — Optimistic concurrency (FR-032 / SC-011)

**Purpose**: Changes the shared profile-draft save contract (`DraftUpdate` + `ProfileDetail`) that every
editor-touching story (US1, US2, US3, US5) saves through. Doing it first means later editor work targets
the final contract. Independently testable (SC-011).

**⚠️ Complete before the editor-modifying user stories.**

- [X] T005 [P] Write FAILING pytest in `apps/api/tests/test_profile_concurrency.py`: concurrent double-save → 409; reload-then-save succeeds; `publish`/`rollback` advance `draft_revision`; omitted `expected_revision` = unconditional save (backward compat); 0-rowcount with missing row → 404 (not 409)
- [X] T006 Create additive migration `apps/api/migrations/versions/0016_agent_profile_draft_revision.py` — `ADD COLUMN draft_revision INTEGER NOT NULL DEFAULT 1` (downgrade drops it; no backfill)
- [X] T007 Add `draft_revision: Mapped[int]` to `AgentProfile` in `apps/api/src/usan_api/db/models.py`
- [X] T008 Add `expected_revision: int | None = None` to `DraftUpdate` and `draft_revision: int` to `ProfileDetail`/`ProfileSummary` in `apps/api/src/usan_api/schemas/agent_profile.py`
- [X] T009 In `apps/api/src/usan_api/repositories/agent_profiles.py`: add `StaleDraftError`; make `update_draft` a guarded conditional UPDATE (`WHERE id=:id AND draft_revision=:expected`, set `updated_at` explicitly since bulk update bypasses `onupdate`); `rowcount==0` + row exists → raise `StaleDraftError`; increment `draft_revision` in `update_draft`, `publish`, and `rollback`
- [X] T010 In `apps/api/src/usan_api/routers/admin_profiles.py`: pass `expected_revision` into `update_draft`; map `StaleDraftError` → `HTTPException(409, …)` with a generic (PHI-free) reload message; re-SELECT to disambiguate 404 vs 409
- [X] T011 [P] Add `draft_revision`/`expected_revision` to the profile types in `apps/admin-ui/src/types/api.ts`
- [X] T012 Send `expected_revision` from `useSaveDraft` in `apps/admin-ui/src/features/editor/hooks.ts`
- [X] T013 Add a 409 reload-warning branch BEFORE the 422 field-mapping in `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx` (and `PublishDialog.tsx`): banner + Reload that confirms-before-discard, invalidates `profileKey(id)`, and `form.reset`s to the fresh draft
- [X] T014 [P] Vitest for the 409 reload-warning UX (no silent local clobber) in `apps/admin-ui/src/test/ProfileEditorConcurrency.test.tsx`

**Checkpoint**: Two-session edit conflict yields 409 + reload; baseline draft/publish/version still works.

---

## Phase 3: User Story 1 — Inline variable declaration (Priority: P1) 🎯 MVP

**Goal**: Declare/manage prompt variables inline from the editor; warnings self-clear; delete-guard lists references.

**Independent Test**: Type undeclared `{{tokens}}`, declare them inline (no navigation), watch warnings clear; PHI badge shows in the palette; deleting a referenced custom lists where it is used.

### Tests for User Story 1 ⚠️ (write first, confirm RED)

- [X] T015 [P] [US1] API tests for `GET /v1/admin/custom-variables/{id}/references` (scans `draft_config` AND `agent_profile_versions.config`; exact `_TOKEN_RE` match not substring; returns names/locations only — no prompt text/values) in `apps/api/tests/test_custom_variable_references.py`
- [X] T016 [P] [US1] Vitest for inline declare: per-token "Declare" chip + "Declare all remaining", warning self-clears after create, builtin-collision blocked, PHI badge in palette, AND the Monaco textarea-fallback path renders the chips — in `apps/admin-ui/src/test/PromptEditorInlineDeclare.test.tsx`

### Implementation — API delete-guard (FR-007)

- [X] T017 [US1] Add `CustomVariableReferences` response schema in `apps/api/src/usan_api/schemas/custom_variables.py`
- [X] T018 [US1] Add the reference-scan query (JSONB `::text ILIKE '%{{name}}%'` prefilter then exact `_TOKEN_RE` confirm over the 8 prompt fields + SMS bodies, across `agent_profiles.draft_config` and `agent_profile_versions.config`) in `apps/api/src/usan_api/repositories/custom_variables.py`
- [X] T019 [US1] Add `GET /v1/admin/custom-variables/{variable_id}/references` (admin-session; 404 if missing; names/locations only) in `apps/api/src/usan_api/routers/admin_custom_variables.py`

### Implementation — admin-ui inline declaration (FR-001–FR-006, FR-008)

- [X] T020 [P] [US1] Extract the private create dialog from `CustomVariablesPage` into a shared, name-prefillable `apps/admin-ui/src/features/customVariables/DeclareVariableDialog.tsx` (optional read-only `name` prop; reuses `components/ui/dialog.tsx`)
- [X] T021 [US1] Replace the static unknown-token paragraph in `apps/admin-ui/src/features/editor/sections/PromptEditor.tsx` with per-token "Declare" chips + "Declare all remaining" (reuse `useCreateCustomVariable`; the `["variable-catalog"]` invalidation auto-clears the warning + Monaco decoration — FR-003)
- [X] T022 [P] [US1] Add a PHI badge per custom entry in `apps/admin-ui/src/features/editor/sections/VariablePalette.tsx` (FR-005); confirm built-in/custom grouping
- [X] T023 [P] [US1] Client-side builtin-collision mirror (derive builtin names from the live catalog tier) in `DeclareVariableDialog`, deferring to the server 422/409 as authoritative (FR-006)
- [X] T024 [US1] Add `useCustomVariableReferences(id)` in `apps/admin-ui/src/features/customVariables/hooks.ts` and wire the `ConfirmDialog` in `CustomVariablesPage.tsx` to list referencing profiles/locations before delete (FR-007)

**Checkpoint**: A new variable is declared and its warning cleared entirely within the editor (SC-001, SC-002).

---

## Phase 4: User Story 2 — Voice / LLM / STT pickers (Priority: P2)

**Goal**: Curated, searchable voice picker with audio preview; curated LLM/STT selects; unsupported selections blocked on save without breaking published versions.

**Independent Test**: Pick a voice and hear a sample (<30s, no typing); choose LLM/STT from lists; saving an unsupported id → field-level 422; a published version with a withdrawn id still loads.

### Tests for User Story 2 ⚠️ (write first, confirm RED)

- [ ] T025 [P] [US2] API tests in `apps/api/tests/test_catalog_endpoints.py`: `GET /voice-catalog` + `GET /model-catalog`; sample endpoint streams `audio/mpeg` for a catalog id and 404s otherwise; assert ONLY `SAMPLE_PHRASE` can reach synthesis (no contact field path); handler-layer 422 for unsupported voice/model in `update_draft`/`publish`/`rollback`; a frozen config with a withdrawn id still deserializes (`test_legacy_config_still_deserializes` stays green)
- [ ] T026 [P] [US2] Vitest in `apps/admin-ui/src/test/VoiceModelPickers.test.tsx`: voice search + play control; LLM/STT curated selects; deprecation marker for withdrawn values; Zod stays permissive

### Implementation — API catalogs (FR-009–FR-014)

- [ ] T027 [P] [US2] New `apps/api/src/usan_api/schemas/voice_catalog.py` (`VoiceSpec`, `VOICE_CATALOG`, `VOICE_IDS`, `SAMPLE_PHRASE` constant, `VoiceCatalogResponse`) mirroring `tool_catalog.py`
- [ ] T028 [P] [US2] New `apps/api/src/usan_api/schemas/model_catalog.py` (`ModelSpec`, `MODEL_CATALOG`, `LLM_MODEL_NAMES`, `STT_MODEL_NAMES`, `ModelCatalogResponse`; seed Vertex Gemini ids + `ink-whisper`)
- [ ] T029 [US2] New `apps/api/src/usan_api/routers/admin_voice_catalog.py`: `GET /v1/admin/voice-catalog` + `GET /v1/admin/voice-catalog/{voice_id}/sample` (httpx → Cartesia `/tts/bytes` with `SAMPLE_PHRASE`, cache bytes per `(voice_id, model)`, `StreamingResponse` audio/mpeg, `require_admin_session` + rate limiter)
- [ ] T030 [P] [US2] New `apps/api/src/usan_api/routers/admin_model_catalog.py`: `GET /v1/admin/model-catalog`
- [ ] T031 [US2] Register both routers in `apps/api/src/usan_api/main.py` (beside `admin_tool_catalog.router`)
- [ ] T032 [US2] Add `model_catalog_violations(...)` and voice-membership-violation helpers in `apps/api/src/usan_api/schemas/agent_config.py` (handler-layer, fabricated field-level `loc`; do NOT add `Literal`/enum to the frozen `LLMConfig`/`STTConfig`/`VoiceConfig` fields — preserve the forward-compat invariant)
- [ ] T033 [US2] Wire voice + model validation into `update_draft`, `publish`, and `rollback` in `apps/api/src/usan_api/routers/admin_profiles.py` (alongside `custom_phi_sms_violations`)

### Implementation — admin-ui pickers

- [ ] T034 [P] [US2] New `apps/admin-ui/src/config/voiceCatalog.ts` (`useVoiceCatalog`) and `apps/admin-ui/src/config/modelCatalog.ts` (`useModelCatalog`), 5-min staleTime, mirroring `toolCatalog.ts`
- [ ] T035 [US2] Rebuild `apps/admin-ui/src/features/editor/sections/VoiceSection.tsx`: searchable curated picker (language/gender/style) + per-voice play button hitting the sample endpoint + deprecation handling (FR-009, FR-010)
- [ ] T036 [P] [US2] Convert `apps/admin-ui/src/features/editor/sections/LLMSection.tsx` and `STTSection.tsx` to curated kind-filtered selects with a deprecation marker; keep `model`/voice as `str` in `apps/admin-ui/src/config/agentConfigSchema.ts`; update `apps/admin-ui/src/config/fieldMeta.ts` help text

**Checkpoint**: Voice/model are chosen from curated lists with audio preview; unsupported selections blocked (SC-003, SC-004).

---

## Phase 5: User Story 3 — Understandable & editable defaults (Priority: P3)

**Goal**: Defaults page states what runs per direction, explains the resolution order, shows the built-in fallback read-only, and links to edit the default profile.

**Independent Test**: From Defaults alone, state what runs for an unassigned inbound/outbound call; edit the default profile via the link and see it take effect; archiving the default surfaces a warning.

### Tests for User Story 3 ⚠️ (write first, confirm RED)

- [ ] T037 [P] [US3] Vitest in `apps/admin-ui/src/test/DefaultsPage.test.tsx`: renders per-direction current default, plain-language resolution order, read-only built-in fallback, edit-link to the default profile, and an ineligible-default (archived/unpublished) warning + replacement prompt

### Implementation (FR-016–FR-020)

- [ ] T038 [US3] Add a read-only defaults endpoint `GET /v1/admin/defaults` in `apps/api/src/usan_api/routers/admin_profiles.py` (or a sibling router) returning the per-direction default profile ids/names + the built-in `DEFAULT_AGENT_CONFIG` (read-only) + a resolution-order descriptor — names/non-PHI only
- [ ] T039 [US3] Rework `apps/admin-ui/src/features/defaults/DefaultsPage.tsx`: per-direction current default, plain-language resolution order (override → contact assignment → per-direction default → built-in fallback), read-only fallback panel, edit-link to the chosen default profile, ineligible-default warning + replacement prompt

**Checkpoint**: An admin can correctly state what runs for an unassigned call and edit the default (SC-005, SC-006).

---

## Phase 6: User Story 4 — Generic naming "Contacts" (Priority: P3)

**Goal**: No user-facing "elder/elders" remains; a `contact_name` builtin aliases `elder_name`; all external contracts stay intact.

**Independent Test**: Every admin screen reads "Contact(s)"; `{{contact_name}}` is recognized; `/v1/admin/elders`, webhook `elder_id`, and published `{{elder_name}}` snapshots all still work.

### Tests for User Story 4 ⚠️ (write first, confirm RED)

- [ ] T040 [P] [US4] Cross-layer tests: `contact_name` resolves identically to `elder_name` in `apps/api/tests/test_builtin_vars.py` AND the agent mirror `services/agent/tests/test_prompt_vars.py`; update `apps/admin-ui/src/test/NavSidebar.test.tsx` to assert the "Contacts" label + `/contacts` href. **(G2 / FR-022, SC-008)** Add a backward-compat regression test in `apps/api/tests/test_elders_backcompat.py` asserting the legacy recipient surfaces still work post-rename: `/v1/admin/elders` (and `/v1/elders` CRUD) respond unchanged, and the outbound webhook payload still carries the `elder_id` key (no route/field rename).

### Implementation — `contact_name` builtin (FR-024) + relabel (FR-021)

- [ ] T041 [US4] Add the `contact_name` `VariableSpec` (tier=builtin, phi=false, default "there") adjacent to `elder_name` in `apps/api/src/usan_api/schemas/variable_catalog.py`
- [ ] T042 [US4] Add `"contact_name"` to `DATA_BUILTIN_NAMES` and stamp `resolved["contact_name"] = full` (same source as `elder_name`) in `apps/api/src/usan_api/builtin_vars.py`
- [ ] T043 [US4] Add `"contact_name": "there"` to the `BUILTIN_DEFAULTS` mirror in `services/agent/src/usan_agent/prompt_vars.py` (lockstep with T041/T042)
- [ ] T044 [P] [US4] User-facing relabel sweep to "Contact/Contacts": `apps/admin-ui/src/components/NavSidebar.tsx` (label + `to: '/contacts'`), `apps/admin-ui/src/routes.tsx` (path `/elders`→`/contacts`), `features/elders/EldersPage.tsx`, `features/defaults/DefaultsPage.tsx`, `features/calls/CallsPage.tsx` + `CallDetailPage.tsx`, `features/queues/QueueTable.tsx`, `features/profiles/ProfilesListPage.tsx`, `config/fieldMeta.ts` — KEEP internal identifiers and the literal `{elder_name}`/`elder_name` token references
- [ ] T045 [US4] Add a deploy-time guard: warn (name-only log) if a custom variable named `contact_name` already exists so it isn't silently shadowed — in `apps/api/src/usan_api/repositories/custom_variables.py` startup check or a migration note

**Checkpoint**: Zero user-facing "elder" remains (SC-007); external contracts unbroken (SC-008).

---

## Phase 7: User Story 5 — Test an agent before publishing (Priority: P4)

**Goal**: Text simulation (Vertex-direct, stubbed tools) and a browser-webcall audio test, both sandboxed — no PSTN, no production records, no real PHI.

**Independent Test**: Run Test LLM and Test Audio on a draft; observe expected behavior; confirm no `Call`/wellness/audit row was created and only synthetic sample vars were used.

### Tests for User Story 5 ⚠️ (write first, confirm RED)

- [ ] T046 [P] [US5] API tests in `apps/api/tests/test_profile_tests.py`: `POST .../test/llm` runs Vertex-direct with stub tools and writes no DB rows; `POST .../test/audio` mints a join-only short-TTL token + dispatches `session_kind="test"`; viewers get 403; **(C1)** each test invocation records exactly one PHI-free audit/log entry (actor + profile + `kind`, with no sample-var values)
- [ ] T047 [P] [US5] Agent tests in `services/agent/tests/test_test_session.py`: `session_kind=="test"` writes no `Call`/wellness/medication/audit row, makes no `/v1/tools/*` call, starts no egress, has no SIP; only the no-op tool registry is reachable; waits for a participant generically (no `sip.*` reads). **(G1 / FR-015)** Also assert the test session builds the pipeline with the draft `test_config`'s selected voice (`voice.cartesia_voice_id`), `llm.model`, and `stt.model` — i.e. a test uses exactly the chosen voice/models, the same guarantee as a live call.
- [ ] T048 [P] [US5] Contract test asserting the api-side prompt substitutor matches the agent's `prompt_vars.substitute` on a shared corpus, in `apps/api/tests/test_prompt_substitution_parity.py`

### Implementation — API (FR-025–FR-027)

- [ ] T049 [US5] New `apps/api/src/usan_api/schemas/profile_tests.py`: `TestLlmRequest/Response`, `TestAudioRequest/Response`
- [ ] T050 [US5] New `apps/api/src/usan_api/prompt_substitution.py` — api-side parallel copy of the agent's `substitute`/`build_vars` (Service Isolation; guarded by T048)
- [ ] T051 [US5] In `apps/api/src/usan_api/livekit_dispatch.py`: add `mint_browser_token(...)` (`VideoGrants(room_join, can_publish, can_subscribe)`, short TTL) and `dispatch_test_agent(...)` (embeds `session_kind="test"` + inline `test_config` + sample vars in metadata)
- [ ] T052 [US5] New `apps/api/src/usan_api/routers/admin_profile_tests.py`: `POST /v1/admin/profiles/{id}/test/llm` (Vertex via ADC, stub tools from `TOOL_CATALOG`, bounded multi-turn loop) + `POST .../test/audio`; `require_admin_role(ADMIN)`; reuse voice/model validation on the draft. **(C1 / FR-029, Constitution VI)** Each test invocation MUST emit a structured audit/log entry (actor email + profile id + `kind="test_llm"|"test_audio"`, PHI-free — never sample-var values) so live-provider test usage is observable; cover this in T046.
- [ ] T053 [US5] Register the test router in `apps/api/src/usan_api/main.py`

### Implementation — agent sandbox (FR-027, FR-028)

- [ ] T054 [US5] In `services/agent/src/usan_agent/worker.py`: extend `CallMetadata` + `parse_metadata` with `session_kind`/`test_config`; branch `entrypoint()` for test mode (build `AgentConfig` from `test_config`; skip inbound-lookup/transcript-flush/metrics-flush/recording/SIP; generic `wait_for_participant`)
- [ ] T055 [US5] In `services/agent/src/usan_agent/check_in.py`: add a no-op `_TEST_TOOL_REGISTRY` selected when `session_kind=="test"` (stub `@function_tool` callables that never call `api_client`)

### Implementation — admin-ui (FR-025, FR-028)

- [ ] T056 [P] [US5] Add the test endpoints to `apps/admin-ui/src/lib/api.ts` and build a `TestLLMPanel` chat component under `apps/admin-ui/src/features/editor/`
- [ ] T057 [US5] Build `TestAudioPanel` (livekit-client `Room.connect` + mic publish + subscribed-audio playback) and wire both panels into `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx`

**Checkpoint**: A draft can be tested (text + audio) with zero production records and no real PHI (SC-009).

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T058 [P] Viewer permission sweep (FR-030): confirm catalog GETs are admin-session, all mutations/tests require admin role, and the admin-ui hides mutate/test actions for viewers; add tests
- [ ] T059 [P] Verify field-level error mapping for the new voice/model 422 `loc`s in `apps/admin-ui/src/lib/` `tryParseFieldErrors` (FR-031)
- [ ] T060 [P] Observability/audit pass (FR-029, Constitution VI): config-affecting actions audited; confirm no PHI in logs, the 409 detail, or audit entries
- [ ] T061 [P] Docs: relabel curl examples in `infra/README.md`, document the new env keys + deploy ordering, and update `docs/` as needed
- [ ] T062 Run all gates: `cd apps/api && uv run pytest --cov` (≥80%) + `ruff check .` + `uv run mypy`; `cd services/agent && uv run pytest && ruff check .`; `cd apps/admin-ui && npm test && npm run build`; then execute `quickstart.md` scenarios 1–6

---

## Dependencies & Execution Order

- **Setup (Phase 1)** → no dependencies; start immediately.
- **Foundational (Phase 2, concurrency)** → depends on Setup; **blocks** the editor-modifying stories (US1/US2/US3/US5) so they target the final save contract. (US4 is independent of it.)
- **User Stories (Phases 3–7)** → after Foundational:
  - **US1 (P1)** — independent (delete-guard API + editor composition).
  - **US2 (P2)** — independent; its handler-layer validation lands in the same three `admin_profiles.py` handlers touched by Phase 2 (sequence T033 after T010 to avoid a merge clash).
  - **US3 (P3)** — independent UI + one read endpoint.
  - **US4 (P3)** — fully independent of Phase 2; the three `contact_name` mirror edits (T041–T043) must land together.
  - **US5 (P4)** — independent; largest; spans api + agent + admin-ui.
- **Polish (Phase 8)** → after all desired stories.

### Within each story
Tests (write first, RED) → models/schemas → repositories/services → endpoints/dispatch → UI → integration. Mirror edits (T041–T043) and the substitutor parity (T048↔T050) must stay in lockstep.

## Parallel Opportunities

- **Setup**: T001, T002, T004 in parallel (T003 edits settings alone).
- **Phase 2**: T005 + T011 + T014 (tests/types) parallel; implementation T006→T010 sequential (same files), then T012/T013.
- **US1**: T015 + T016 (tests) parallel; T020, T022, T023 parallel (different files); T017→T018→T019 sequential (API chain); T021/T024 after their deps.
- **US2**: T025 + T026 parallel; T027 + T028 + T030 + T034 parallel (new files); T029→T031, T032→T033 sequential; T035/T036 after T034.
- **US5**: T046 + T047 + T048 parallel; T049/T050/T051 parallel-ish (different files) → T052→T053; T054/T055 (agent) parallel with API; T056/T057 after schemas.
- **Cross-story**: with capacity, US1, US3, US4 can proceed in parallel after Phase 2; US2 and US5 both touch `admin_profiles.py`/`main.py` (serialize those specific edits).

## Implementation Strategy

- **MVP = Phase 1 + Phase 2 + Phase 3 (US1)** — ships the #1 pain-point fix (inline variables) on a safe save contract. Stop, validate (quickstart Scenario 1 + 6), demo.
- **Increment 2 = US2** (voice/model pickers) — the most visible RetellAI-parity upgrade.
- **Increment 3 = US3 + US4** (defaults clarity + Contacts rename) — both low-risk P3s.
- **Increment 4 = US5** (agent testing) — closes the author→test→publish loop.
- **Finish** with Phase 8 gates + full quickstart run.

## Notes

- [P] = different files, no incomplete dependency.
- Tests are written FIRST and must FAIL before implementation (Constitution IV).
- Never add `Literal`/enum to the frozen `AgentConfig` voice/model fields — validate at the handler layer (preserves the published-version forward-compat invariant).
- Keep `apps/api` and `services/agent` import-isolated; the test-mode draft reaches the agent only via LiveKit dispatch metadata.
- New secrets (`CARTESIA_API_KEY`, `GCP_PROJECT`, `VERTEX_LOCATION`) must be in the VM `.env` BEFORE the deploy tag.
- Commit after each task or logical group; keep mypy + ruff green per commit.
