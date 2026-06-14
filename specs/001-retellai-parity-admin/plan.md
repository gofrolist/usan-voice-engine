# Implementation Plan: RetellAI-Parity Admin Console & Agent Studio

**Branch**: `001-retellai-parity-admin` | **Date**: 2026-06-13 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-retellai-parity-admin/spec.md`

## Summary

Bring the USAN admin console + API to functional parity with RetellAI for configuring voice agents,
without breaking live integrations. Five prioritized capabilities: (P1) declare/manage prompt variables
**inline** from the editor; (P2) choose voices/LLMs/STT from **curated pickers** (voices with audio
preview) instead of free-text identifiers; (P3) make defaults **understandable and editable** (the
per-direction default *profile* is the single source of truth; the built-in fallback is shown read-only);
(P3/US4) rename the user-facing domain term **"elder" → "contacts"** via a backward-compatible shim;
(P4) **test an agent before publishing** (text simulation + a browser-webcall audio test). Technical
approach (from [research.md](./research.md)): the editor is already structurally complete
(draft/publish/versioning + a frozen `AgentConfig` JSONB + a catalog-as-code pattern), so the work is
curation, validation, and friction removal. New catalogs (voice, model) ship as code constants exposed
via `GET /v1/admin/*-catalog`; selection is validated at the **handler layer** to preserve the
`agent_profile_versions` forward-compat invariant; optimistic concurrency adds one column
(`draft_revision`); the rename is UI-relabel-only plus a new `contact_name` builtin aliasing
`elder_name`; agent testing reuses the existing LiveKit dispatch path with a `session_kind="test"`
sandbox and a Vertex-direct text path in the API.

## Technical Context

**Language/Version**: Python 3.14 (`apps/api`, uv), Python 3.12 (`services/agent`, uv),
TypeScript 5.5 / React 18 (`apps/admin-ui`, Vite 6).

**Primary Dependencies**: FastAPI + SQLAlchemy (async) + Pydantic v2 + Alembic (api); LiveKit Agents
1.x + Cartesia/Google plugins (agent); React Query + React-Hook-Form + Zod + Tailwind + Monaco
(admin-ui). NEW: `google-genai`/vertexai in `apps/api` (text-test LLM path); `livekit-client` in
`apps/admin-ui` (audio test); `httpx` already present (voice-sample proxy).

**Storage**: PostgreSQL 16 + pgvector. One additive column this feature
(`agent_profiles.draft_revision`); no new tables. Voice/model catalogs are code constants, not tables.

**Testing**: pytest (api + agent, ≥80% coverage, mypy + ruff in CI); Vitest + Testing Library
(admin-ui). New cross-layer/contract tests for the `contact_name` mirror and the api↔agent token
substitutor parity.

**Target Platform**: Single GCP Compute Engine VM, Docker Compose. Admin-ui is a browser SPA served via
Caddy; API + agent are containers; LiveKit + livekit-sip run host-networking in prod.

**Project Type**: Web application — three deployable units (`apps/api`, `apps/admin-ui`,
`services/agent`) that do not import across the api/agent boundary.

**Performance Goals**: Editor interactions feel instant (inline declare clears warnings on the next
render); a voice sample plays in <30s including first-time synthesis (SC-003); test sessions are bounded
(text iteration cap; audio `max_call_duration_s` + short token TTL).

**Constraints**: PHI must not egress to non-BAA infra (Vertex-only LLM, fixed PHI-free sample phrase,
synthetic-only test data); `apps/api` and `services/agent` must not import each other; new published
configs must never break re-validation of older frozen versions; new secrets must reach the VM `.env`
before the deploy tag.

**Scale/Scope**: Low-concurrency admin tool (a handful of operators); thousands of contacts; tens of
profiles. ~2 new DB-free catalogs, ~6 new/changed API endpoints, ~1 migration, ~3 additive agent-mirror
edits, and editor/section UI rework across `apps/admin-ui`.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.0. Re-checked after Phase 1 design.*

| Principle | Assessment | Status |
|-----------|-----------|--------|
| **I. Service Isolation** | No api↔agent import added. Agent receives the *draft* config via LiveKit dispatch metadata (existing channel); the text-test substitutor is a maintained parallel copy of `prompt_vars`, guarded by a contract test. | ✅ PASS |
| **II. PHI Containment (NON-NEGOTIABLE)** | Voice samples use a fixed PHI-free phrase (module constant). Test sessions use synthetic/admin-supplied values only — never auto-load contact PHI; no production records written. Both the agent and the new API text-test LLM path use **Vertex AI via ADC**, not the Gemini Developer API. Reference/audit detail stays names/flags only. | ✅ PASS |
| **III. Type Safety & Validated Contracts** | All new config/catalog/request bodies are Pydantic v2 models; new column + fields fully typed; mypy + ruff gate in CI. Catalog selection validated server-side. | ✅ PASS |
| **IV. Test-First (NON-NEGOTIABLE)** | Each work item lands tests first (RED→GREEN), ≥80% coverage; new contract tests for mirror parity and sandbox completeness. | ✅ PASS (commitment, enforced in tasks) |
| **V. Idempotent Outbound Operations** | No change to outbound-call dispatch/DNC/retry. Audio test is a browser webcall — places no PSTN call, creates no `Call` row, consumes no number. | ✅ PASS (N/A surface) |
| **VI. Observability** | Config changes already audited in the profile router; new mutations (catalog-validation rejects, test dispatch) logged structurally; conflict/audit messages carry no PHI. | ✅ PASS |
| **VII. Simplicity & YAGNI** | Catalogs are code constants (no DB, no in-product editor); rename is relabel-only (no route alias); test/inline-declare reuse existing endpoints/dispatch; one additive column. | ✅ PASS |
| **Security & Compliance** | New `CARTESIA_API_KEY` / `GCP_PROJECT` / `VERTEX_LOCATION` as env/secrets (SecretStr where secret), in VM `.env` before deploy tag. New endpoints behind `require_admin_session` (+ admin role for mutations/tests); LiveKit browser tokens are short-TTL, join-only, single throwaway room; sample proxy under existing rate limiting. | ✅ PASS |

**Result**: No violations. Complexity Tracking table is empty (nothing to justify).

## Project Structure

### Documentation (this feature)

```text
specs/001-retellai-parity-admin/
├── plan.md              # This file (/speckit-plan output)
├── spec.md              # Feature spec (/speckit-specify, clarified)
├── research.md          # Phase 0 output (6 grounded decisions)
├── data-model.md        # Phase 1 output (entities, fields, transitions)
├── quickstart.md        # Phase 1 output (validation scenarios)
├── contracts/           # Phase 1 output (API + agent-dispatch contracts)
│   ├── admin-api.md      # New/changed REST endpoints
│   └── agent-test-session.md  # Test-mode dispatch metadata contract
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
apps/api/                              # FastAPI (Python 3.14, uv)
├── src/usan_api/
│   ├── schemas/
│   │   ├── voice_catalog.py           # NEW — VoiceSpec + VOICE_CATALOG + sample phrase
│   │   ├── model_catalog.py           # NEW — ModelSpec + MODEL_CATALOG (llm/stt)
│   │   ├── agent_config.py            # +model_catalog_violations, +voice membership check (handler-layer)
│   │   ├── agent_profile.py           # DraftUpdate.expected_revision; ProfileDetail/Summary.draft_revision
│   │   ├── variable_catalog.py        # +contact_name builtin
│   │   ├── custom_variables.py        # +CustomVariableReferences response
│   │   └── profile_tests.py           # NEW — TestLlmRequest/Response, TestAudioRequest/Response
│   ├── routers/
│   │   ├── admin_voice_catalog.py     # NEW — GET catalog + GET /{id}/sample (audio proxy)
│   │   ├── admin_model_catalog.py     # NEW — GET model catalog
│   │   ├── admin_profile_tests.py     # NEW — POST .../test/llm, POST .../test/audio
│   │   ├── admin_profiles.py          # wire catalog validation + 409 (StaleDraftError) + expected_revision
│   │   ├── admin_custom_variables.py  # +GET /{id}/references
│   │   └── ...
│   ├── repositories/
│   │   ├── agent_profiles.py          # guarded UPDATE + StaleDraftError; bump draft_revision in publish/rollback
│   │   └── custom_variables.py        # +reference-scan query
│   ├── builtin_vars.py                # stamp resolved["contact_name"] = full
│   ├── prompt_substitution.py         # NEW — api-side parallel copy of agent prompt_vars.substitute
│   ├── livekit_dispatch.py            # +mint_browser_token, +dispatch_test_agent
│   └── settings.py                    # +CARTESIA_API_KEY, +GCP_PROJECT, +VERTEX_LOCATION, +CARTESIA_* sample cfg
└── migrations/versions/
    └── 0016_agent_profile_draft_revision.py  # NEW — additive column

services/agent/                        # LiveKit Agents (Python 3.12, uv)
└── src/usan_agent/
    ├── worker.py                      # CallMetadata.session_kind/test_config; entrypoint test branch
    ├── check_in.py                    # no-op _TEST_TOOL_REGISTRY when session_kind=="test"
    └── prompt_vars.py                 # +contact_name in BUILTIN_DEFAULTS (mirror)

apps/admin-ui/                         # React + TS (Vite)
└── src/
    ├── config/
    │   ├── voiceCatalog.ts            # NEW — useVoiceCatalog()
    │   ├── modelCatalog.ts            # NEW — useModelCatalog()
    │   └── agentConfigSchema.ts       # keep model/voice as str (permissive for frozen values)
    ├── features/editor/sections/
    │   ├── PromptEditor.tsx           # per-token Declare chips + Declare-all
    │   ├── VariablePalette.tsx        # PHI badge per entry
    │   ├── VoiceSection.tsx           # searchable picker + play sample
    │   ├── LLMSection.tsx / STTSection.tsx  # curated <select>
    ├── features/customVariables/
    │   ├── DeclareVariableDialog.tsx  # NEW — shared, name-prefillable dialog
    │   └── hooks.ts                   # +useCustomVariableReferences
    ├── features/editor/
    │   ├── ProfileEditorPage.tsx      # 409 reload-warning branch; TestLLMPanel/TestAudioPanel
    │   └── hooks.ts                   # useSaveDraft sends expected_revision
    ├── features/defaults/DefaultsPage.tsx   # resolution-order explainer + read-only fallback + edit-link
    ├── features/elders/ + calls/ + queues/ + profiles/ + NavSidebar + routes.tsx  # "Contacts" relabel
    └── package.json                   # +livekit-client
```

**Structure Decision**: Existing 3-unit web-application layout is retained. The api/agent isolation
boundary is preserved; all cross-unit data flows over HTTP (tool/runtime endpoints) and LiveKit dispatch
metadata. New behavior is added by extending existing modules and adding sibling catalog/router files
that mirror the established `tool_catalog.py` / `admin_tool_catalog.py` patterns.

## Complexity Tracking

> No constitution violations — no justifications required.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| _(none)_ | — | — |
