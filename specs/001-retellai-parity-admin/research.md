# Phase 0 Research: RetellAI-Parity Admin Console & Agent Studio

**Feature**: `001-retellai-parity-admin` | **Date**: 2026-06-13 | **Spec**: [spec.md](./spec.md)

This document consolidates the Phase 0 research that resolves the design unknowns surfaced by the
spec. Every decision is grounded in the actual repo (file paths cited) and verified library behavior
(Cartesia, LiveKit, Vertex AI). The recurring theme: **the profile editor is already structurally
complete** (draft/publish/versioning, a frozen `AgentConfig` JSONB with Voice/LLM/STT/Tools/Timing/
Voicemail/SpeechAdvanced/Policy sub-configs, a catalog-as-code pattern for tools and variables).
Most work is *discoverability, curation, validation, and friction removal* — not new plumbing.

---

## R1 — Curated voice catalog + in-browser audio preview (US2 / FR-009, FR-010, FR-014)

**Decision**: Ship a platform-curated voice catalog as a **code-backed config module**
(`apps/api/.../schemas/voice_catalog.py`, mirroring `tool_catalog.py`/`variable_catalog.py`),
exposed read-only via `GET /v1/admin/voice-catalog`, consumed by a new `useVoiceCatalog()` hook that
drives a searchable picker in `VoiceSection.tsx`. Audio preview is served by a **server-side proxy**
endpoint `GET /v1/admin/voice-catalog/{voice_id}/sample` (admin-session-guarded) that lazily
synthesizes a **fixed, PHI-free phrase** via Cartesia `POST /tts/bytes` and caches the bytes per
`(voice_id, model)`. Selection is validated against the catalog at save time (warn-don't-break-published).

**Rationale**:
- Mirrors the proven, constitution-aligned (VII Simplicity) tool/variable catalog pattern: a global
  constant, not a DB table, not an in-product editor — matching the clarification that the catalog is
  platform/engineer-maintained and not admin-editable. As a global constant it stays out of the
  `agent_profile_versions` forward-compat invariant.
- The agent already passes `VoiceConfig.cartesia_voice_id/tts_model/speed/language` verbatim into
  `cartesia.TTS(**kwargs)` (`services/agent/.../pipeline.py`), so this is purely an API-validation +
  UI change. **No agent change** → Service Isolation (I) preserved.
- Verified via Context7 (official Cartesia OpenAPI): `GET /voices` carries id/name/description/gender/
  language; `POST /tts/bytes` generates audio. Both require the secret `sk_car_` key, so the browser
  must **not** call Cartesia directly — the API proxies it (Security & Compliance: secrets stay server-side).
- The fixed sample phrase is a module constant (never a contact name/transcript), making PHI leakage
  into the sample path structurally impossible (II PHI Containment).

**Alternatives considered**: pre-rendered static clips committed to the repo (binary bloat, no
StaticFiles mount); browser calls Cartesia directly (leaks the API key); DB-backed catalog with admin
CRUD (out of scope per clarification); live unfiltered `/voices` list (spec requires a *curated*
allow-list); agent generates samples (would force api→agent import, violating I).

**Key risks**: `CARTESIA_API_KEY` is a **new secret the API does not hold today** — must be `SecretStr`
via Secret Manager and present in the VM `.env` **before** the deploy tag (per the deploy-env memory).
First-play cold-start latency (one synthesis round-trip) — acceptable for SC-003 (<30s) with a loading
state; cache makes cost once-per-voice. Keep the proxy behind `require_admin_session` + rate limiter so
it can't be abused to rack up TTS spend.

---

## R2 — Curated LLM + STT model catalog (US2 / FR-011, FR-012, FR-013, FR-014)

**Decision**: Add a code-backed model catalog (`apps/api/.../schemas/model_catalog.py`) with
`ModelSpec(id, label, description, kind: "llm"|"stt", provider, deprecated, default)` and derived
`LLM_MODEL_NAMES`/`STT_MODEL_NAMES` frozensets, exposed via `GET /v1/admin/model-catalog`. Validate
selection-from-catalog at the **handler layer** (`update_draft`/`publish`/`rollback` in
`admin_profiles.py`) — **not** in the frozen `AgentConfig` field validators — exactly reusing the
`custom_phi_sms_violations` pattern (fabricated field-level `loc` so the client parses it like a
pydantic 422). The admin-ui swaps the free-text `LLMSection`/`STTSection` inputs for curated `<select>`s.

**Rationale**:
- The `AgentConfig` FORWARD-COMPATIBILITY INVARIANT requires any tightened constraint to remain
  satisfiable by older frozen `agent_profile_versions.config` rows (re-validated on every read; covered
  by `test_legacy_config_still_deserializes`). Adding a `Literal`/enum to `LLMConfig.model` would 500
  on read for a published version referencing a withdrawn model. **Therefore validate at the handler,
  keep Zod/field as `str`.** This implements FR-014 both ways: blocks saving a *new* unsupported value,
  yet keeps published versions valid (FR-023).
- The agent already runs the LLM on **Vertex AI via ADC** (`google.LLM(vertexai=True, project,
  location)` in `pipeline.py`), satisfying II PHI Containment. Plugin model IDs are `str` passthroughs,
  so the catalog is a server-side allow-list decoupled from the plugin's `Literal`. No agent change.
- Seed LLM: `gemini-3.1-flash-lite` (current default), `gemini-2.5-flash`, `gemini-2.5-flash-lite`,
  `gemini-2.5-pro` (all Vertex-served). Seed STT: `ink-whisper` (sole Cartesia STT model).

**Alternatives considered**: enum on `LLMConfig.model` (breaks forward-compat → 500 on read); bind to
the LiveKit plugin `Literal` (omits the current default, lags Vertex); DB-backed catalog (out of scope);
publish-only validation (spec wants the *save* blocked — FR scenario fires on save).

**Resolved open question**: per-model temperature min/max metadata is **deferred** — `LLMConfig.temperature`
already enforces 0.0–2.0; FR-013 range hints come from static `fieldMeta` for now.

**Key risks**: seed IDs MUST be Vertex-served names (not Gemini Developer API names) or II regresses.
Deprecation UX: a frozen version's withdrawn model must render (read) but block re-save — Zod stays
permissive, picker shows a deprecation marker, the handler 422 is authoritative. All **three** handler
call-sites (update_draft/publish/rollback) must gate. mypy must stay green (memory: ci_runs_mypy).

---

## R3 — Agent testing / simulation: Test LLM (text) + Test Audio (browser webcall) (US5 / FR-025–FR-028)

**Decision**: Two test modes sharing one `session_kind` sandbox discriminator, never crossing the
import boundary.

- **Text test** — runs entirely in `apps/api`, no agent, no LiveKit. New `POST
  /v1/admin/profiles/{id}/test/llm` (admin-only): validate the draft via `AgentConfig`, substitute
  admin-supplied `sample_vars` using an **api-side token substitutor that mirrors** the agent's
  `prompt_vars.substitute()` (parallel copy — agent code cannot be imported), call **Vertex AI
  directly** (`vertexai=True` + ADC) with the draft `llm.model`/`temperature`. Tools are presented to
  the model as **schema-only stubs** from `TOOL_CATALOG`; tool calls return a synthetic result string
  and are echoed to the UI — **no** `/v1/tools/*` calls, no DB writes. Bounded multi-turn loop
  (model → stub result → continue) with an iteration cap.
- **Audio test** — browser webcall over WebRTC reusing the existing explicit-dispatch pattern. New
  `POST /v1/admin/profiles/{id}/test/audio` (admin-only): mint a **short-TTL LiveKit browser
  `AccessToken`** (`VideoGrants(room_join, can_publish, can_subscribe)`, ~10–15 min) for a throwaway
  room, create the room, and `create_dispatch(...)` the agent worker with the **draft config embedded
  inline in the dispatch metadata** plus `session_kind="test"` and sample vars. Returns
  `{url, token, room}`. The admin-ui adds `livekit-client` and connects via `Room.connect`.
- **Agent change**: extend `CallMetadata` + `parse_metadata` with `session_kind: "call"|"test"` and an
  optional `test_config`. In `entrypoint()`, when `session_kind=="test"`: build `AgentConfig` from
  `test_config` (skip the published-only resolver), register **no-op tool stubs**, and **skip**
  inbound-lookup / transcript-flush / metrics-flush / recording / SIP. No PSTN participant; the browser
  joins directly (no phone number consumed — FR-028).

**Rationale**:
- Outbound dispatch already injects per-call data via `CreateAgentDispatchRequest` metadata and the
  agent already parses `ctx.job.metadata` — extending that metadata is the lowest-risk, isolation-
  respecting way to deliver a *draft* config without inventing a new channel and without the
  published-only `/v1/runtime/agent-config` resolver.
- FR-026/FR-027/SC-009 are met **structurally**: test code paths never build a `Call` row, never POST
  to `/v1/tools/*`, never start egress, and never call inbound-lookup — so no production
  call/wellness/medication/audit record can be created, and no real contact PHI is auto-loaded.
- Both LLM paths use Vertex AI via ADC (II). LiveKit `AccessToken`+`VideoGrants` and the JS
  `Room.connect` flow verified via Context7.

**Alternatives considered**: run text test in the agent and proxy from api (agent has no HTTP server;
api already validates `AgentConfig`); inject draft via a temp published row (violates draft→publish
model, risks a real call picking it up); reuse real tool callables pointed at a sandbox base URL
(fragile — one miss writes PHI); real PSTN test (rejected by clarification); browser mints its own token
(would leak `LIVEKIT_API_SECRET`); custom WebRTC bridge (YAGNI — LiveKit path already exists).

**Resolved open questions**: text loop is **bounded multi-turn** (cap ~5 iterations/turn). The test
branch waits for a participant **generically** and reads no `sip.*` attributes. The api-side substitutor
is a maintained parallel copy of `prompt_vars` guarded by a **contract test** against the agent copy.

**Key risks**: `apps/api` has **no Vertex/ADC config today** (only the agent does) — the text path
needs `GCP_PROJECT`/`VERTEX_LOCATION` + the `google-genai` (vertexai) dependency added to the API, and
the API container needs ADC with `aiplatform.user`; confirm the API's existing service account (it
signs GCS recording URLs) can be granted Vertex access (new env keys → VM `.env` refresh before deploy).
**Sandbox completeness**: every PHI/record/egress/SIP side-effect must gate on `session_kind=="test"`;
the no-op tool registry must be the *only* registry reachable in test mode. Browser token must grant
only join+publish+subscribe to the single throwaway room, short TTL. The browser-facing `LIVEKIT_URL`
must be the externally reachable `wss://` address (note prod livekit host-networking memory). Tests use
live providers (real cost) — bound by `max_call_duration_s` + a test-concurrency cap.

---

## R4 — Inline variable declaration in the prompt editor (US1 / FR-001–FR-008) — the #1 pain point

**Decision**: Add inline declaration anchored to the **existing** undeclared-token warning in
`PromptEditor.tsx`, reusing the already-built backend and React Query invalidation. No new create/catalog
API. Concretely:

- Replace the static amber paragraph with **per-token "Declare" chips** + a **"Declare all remaining"**
  action. Clicking opens a shared `DeclareVariableDialog` (extracted from `CustomVariablesPage`'s private
  dialog; reuses `components/ui/dialog.tsx`) pre-filled with the token name (read-only) and empty
  description/example/PHI.
- Reuse `useCreateCustomVariable()` verbatim — it already `POST`s `/v1/admin/custom-variables` and on
  success invalidates both `["custom-variables"]` and `["variable-catalog"]`. Because `PromptsSection`
  derives `knownNames`/`phiNames` from `useVariableCatalog()`, the catalog refetch **auto-clears** the
  warning + the Monaco "unknown" decoration on the next render → FR-003 with zero new wiring (delivers
  SC-001 zero-navigations + SC-002 single-screen).
- Add a **PHI badge** per entry in `VariablePalette` (FR-005). The insert-at-cursor palette already exists.
- **Collision** (FR-006): the server already rejects builtin collisions (422) and duplicates (409);
  add a client-side mirror (derive builtins from the live catalog tier) for instant feedback, deferring
  to the server as authoritative.
- **Delete-guard** (FR-007) is the only net-new backend work: a read-only `GET
  /v1/admin/custom-variables/{id}/references` that scans `agent_profiles.draft_config` **and**
  `agent_profile_versions.config` JSONB for the `{{name}}` token across the 8 prompt fields + SMS bodies
  (JSONB `::text ILIKE` prefilter, then exact `_TOKEN_RE` confirm), returning referencing profile
  ids/names/locations (names only — never prompt text or values). The existing `ConfirmDialog` then lists
  where it is used before a (still warn-don't-block) hard delete.

**Rationale**: the create path, builtin-collision rule, and dual invalidation are all already
implemented and tested, so US1's core is a frontend-composition task. Reusing the existing modal (not a
bespoke Monaco popover) is correct because Monaco is lazy-loaded with a plain-textarea fallback — a
Monaco-only affordance would vanish under the fallback/jsdom test path. Custom-variable definitions
carry **no values** (II untouched). The references endpoint must include version snapshots (forward-
compat) and confirm exact token names (avoid `{{state}}` matching `{{state_full}}`).

**Alternatives considered**: bespoke Monaco hover/CodeLens widget (breaks under textarea fallback);
batch declare endpoint (YAGNI — client loop covers "declare all"); hard-block delete (contradicts the
established warn-don't-block posture); denormalized reference join table (premature — read-time JSONB
scan over a small profile set suffices).

**Key risks**: the reference scan MUST include immutable version snapshots or it under-reports;
`::text ILIKE` can false-positive on substrings (confirm with `_TOKEN_RE`); reference detail must stay
name/location-only (II + the §7 "names/flags only, never values" audit rule); declaring inline updates
only the catalog — the prompt edit still saves via the normal draft PUT (concurrency guard unchanged).

---

## R5 — Optimistic concurrency for profile drafts (FR-032 / SC-011)

**Decision**: Add a dedicated monotonic integer column `agent_profiles.draft_revision INTEGER NOT NULL
DEFAULT 1`, carried in the PUT body as `expected_revision`. Every row-mutating path
(`update_draft`/`publish`/`rollback`) increments it in the same transaction. `update_draft` performs a
**guarded conditional UPDATE** (`WHERE id = :id AND draft_revision = :expected`); `rowcount == 0` with
the row still present → new `StaleDraftError` → HTTP **409** with a generic reload-prompt detail. The
editor branches on `err.status === 409` (before the 422 field-mapping), shows a reload affordance that
re-fetches `ProfileDetail` and `form.reset`s. `expected_revision` is optional (omitted = unconditional
save) for backward compatibility; the editor always sends it. Additive migration `0016`.

**Rationale**: a dedicated integer beats reusing `updated_at` because (a) `set_default` already uses a
bulk `update()` and the repo's own comment warns `onupdate=func.now()` does **not** fire for bulk
updates — so the timestamp isn't reliably monotonic; (b) TIMESTAMPTZ round-trips through JSON with
precision/format equality hazards → false 409s; (c) publish/rollback also mutate the row, so the token
must advance on all three paths — a purpose-named column makes that explicit. A body field beats
If-Match/ETag because the shared `lib/api.ts` wrapper reads only `detail` and discards headers; a body
field rides the existing typed `DraftUpdate` contract with zero wrapper changes. 409 matches the
router's three existing 409 cases.

**Alternatives considered**: reuse `updated_at` (bulk-update gap + precision); If-Match header (forces
wrapper rework); `SELECT ... FOR UPDATE` lock (gives blocking, not detect-and-warn UX); JSONB content
hash (opaque, misses publish/rollback that leave `draft_config` byte-identical); SQLAlchemy
`version_id_col` (StaleDataError harder to map across the explicit bulk-update paths).

**Key risks**: Reload must not silently clobber unsaved local edits — add a confirm step (offer to copy
current text) so a silent *server* overwrite isn't traded for a silent *local* loss. Distinguish 409
(revision mismatch) from 404 (not found) by re-SELECT after 0-rowcount. The guarded bulk UPDATE must set
`updated_at` explicitly (bulk bypasses `onupdate`). 409 detail must carry no PHI / no other actor's email.

---

## R6 — Shim-first "elder" → "Contacts" rename (US4 / FR-021–FR-024)

**Decision**: Adopt the repo's own shim-first recommendation
(`docs/superpowers/research/2026-06-10-phase-b-tenancy-research.md` §2b). Three moves:

1. **UI relabel only** — change visible strings/labels/nav/column-headers/help to "Contact/Contacts"
   and rename the React-Router path token `/elders` → `/contacts` (the one user-visible URL fragment).
   **Keep** internal TS identifiers (`useElders`, `ElderSummary`, `elderId`, `ELDERS_KEY`), the
   `features/elders/` dir name, and all `api.get('/v1/admin/elders…')` calls unchanged — not
   user-facing, pure churn/regression risk. **No `/v1/contacts` route alias** (the route name is never
   user-visible; FR-022 only requires the prior route keep working, which relabel-only satisfies).
2. **Add builtin `contact_name`** as a permanent alias of `elder_name`, resolving from the same
   `elder.name` source. Three additive files: add the `VariableSpec` to `variable_catalog.py`; add
   `"contact_name"` to the agent mirror `prompt_vars.py` `BUILTIN_DEFAULTS`; stamp
   `resolved["contact_name"] = full` in `builtin_vars.py`. The admin-ui needs **zero** change — the
   editor's known-set is fetched from `GET /v1/admin/variable-catalog`, so `{{contact_name}}`
   immediately renders as recognized and appears in the palette (FR-024).
3. **Keep everything else physically intact** (FR-022/FR-023/SC-008): `elders` table, `elder_id` FKs,
   webhook `elder_id` payload key, the `{{elder_name}}` builtin, and the legacy single-brace
   `{elder_name}` slot allow-list all stay forever.

**Rationale**: the tenancy research enumerates six frozen surfaces where a physical rename is *breaking*
(published prompt snapshots embed `{{elder_name}}`; webhooks are signed/consumed externally; the DB is
load-bearing). The spec's Assumptions + Out-of-Scope codify shim-first. `contact_name` is the cheapest
satisfaction of FR-024 (3 small additive files), forward-compatible (the catalog is a global constant,
not snapshotted), and II-safe (same `elder.name` value already flowing). Relabel-only over a route alias
serves FR-022/SC-008 with the least surface.

**Alternatives considered**: `/v1/contacts` route alias (dual surface for zero benefit); one-shot
physical rename (breaks live prompts + external consumers — explicitly out of scope); rename internal TS
identifiers (~10 files of pure-risk churn, no acceptance coverage; SC-007 measures user-facing only);
demote `elder_name` from builtin (breaks FR-023 + forward-compat); agent-only `contact_name` (editor
would still flag it "unknown" since the UI reads the API catalog).

**Resolved open questions**: change the route path `/elders` → `/contacts` (internal bookmarks are
low-stakes). Defer a `contact_first_name` alias (FR-024 satisfied by `contact_name`). Keep the rename
**strictly** elder→contact — do **not** broaden into an eldercare-vocabulary pass ("wellness" etc. are
product-feature words, out of FR-021 scope).

**Key risks**: a customer who already declared a *custom* variable named `contact_name` will be shadowed
by the new builtin — `admin_variable_catalog.py` already drops a shadowed custom with a name-only warning
log (fails safe), but check the `custom_variables` table at deploy for an existing row and warn the
operator. The two agent mirrors (`prompt_vars.py` defaults + `builtin_vars.py` resolution) must both gain
`contact_name` or `{{contact_name}}` renders empty agent-side — add a cross-layer test. SC-007 grep
verification must distinguish the **domain word** "elder" (rename) from the **frozen token identifier**
`elder_name`/`{elder_name}` (keep) so QA doesn't over-rename and break the legacy slot.

---

## Cross-cutting decisions

- **No new DB tables**; one additive column (`draft_revision`, migration `0016`). Voice/model catalogs
  are code constants. Test sessions are stateless (write nothing).
- **New API secrets/config**: `CARTESIA_API_KEY` (for the voice-sample proxy) and `GCP_PROJECT` +
  `VERTEX_LOCATION` (for the text-test Vertex call). Both are new keys the API does not hold today and
  MUST be in the VM `.env` before the deploy tag (deploy-env memory + constitution Development Workflow).
- **Service Isolation (I)** preserved everywhere: the agent gains a test-mode branch fed via dispatch
  metadata; the api gains a parallel-copy token substitutor — no cross-import.
- **PHI Containment (II)** preserved: fixed sample phrase, synthetic-only test data, Vertex-only LLM for
  both the agent and the new text-test path.
- **Test-First (IV)**: each work item lands tests first; new cross-layer/contract tests guard the
  `contact_name` mirrors and the api↔agent substitutor parity.
