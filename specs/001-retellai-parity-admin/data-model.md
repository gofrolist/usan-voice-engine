# Phase 1 Data Model: RetellAI-Parity Admin Console & Agent Studio

**Feature**: `001-retellai-parity-admin` | **Date**: 2026-06-13 | **Source**: [spec.md](./spec.md), [research.md](./research.md)

Scope note: this feature adds **one** persisted column. Most "entities" below already exist; the new
entities (voice/model catalog, test session) are **not persisted** — catalogs are code constants and
test sessions are stateless. Persisted PHI shapes are unchanged.

---

## 1. Persisted entities

### 1.1 AgentProfile (existing — one new column)

Table `agent_profiles`. Existing columns: `id`, `name` (unique), `description`, `status`
(active/archived), `draft_config` (JSONB = `AgentConfig`), `published_version` (int, nullable),
`is_default_inbound` (bool), `is_default_outbound` (bool), `created_by`, `updated_by`, `created_at`,
`updated_at`.

| New field | Type | Rules |
|-----------|------|-------|
| `draft_revision` | `INTEGER NOT NULL DEFAULT 1` | Monotonic concurrency token. Incremented by **every** row-mutating path: `update_draft`, `publish`, `rollback`. Surfaced on `ProfileDetail`/`ProfileSummary`. Migration `0016` (additive; existing rows default to 1; no backfill). |

**State transitions** (unchanged except revision bump):
`draft edited` → `update_draft` (guarded on `expected_revision`; bumps `draft_revision`) →
`publish` (freezes `draft_config` into a new `AgentProfileVersion`, advances `published_version`, bumps
`draft_revision`) ↔ `rollback(v)` (copies version `v` back to draft, re-publishes as a new version,
bumps `draft_revision`). `set_default(direction)` toggles `is_default_*` (bulk update — must set
`updated_at` explicitly; does **not** change draft config, so the three paths that bump `draft_revision`
are update_draft/publish/rollback).

### 1.2 AgentProfileVersion (existing — unchanged)

Table `agent_profile_versions` (immutable). `id`, `profile_id` (FK), `version` (int), `config` (JSONB =
frozen `AgentConfig`), `note`, `published_by`, `published_at`; unique `(profile_id, version)`.
**Invariant preserved**: every row is re-validated through `AgentConfig` on read, so no new *required*
field or tightened field constraint may be added to `AgentConfig` (catalog validation lives in handlers,
not field validators).

### 1.3 CustomVariable (existing — unchanged shape)

Table `custom_variables`: `id`, `name` (unique, `^[a-z][a-z0-9_]{0,63}$`), `description`, `example`,
`phi` (bool), timestamps. Definitions carry **no values**. Inline declaration creates rows here via the
existing `POST /v1/admin/custom-variables` (builtin-collision → 422, duplicate → 409). No schema change;
a new **read-only references view** is computed at query time (§2.3), not stored.

### 1.4 Elder / Contact (existing — unchanged)

Table `elders` and all `elder_id` FKs stay physically intact (shim-first rename). Only user-facing
vocabulary changes. No migration.

---

## 2. New non-persisted entities (code constants & computed views)

### 2.1 VoiceSpec / VOICE_CATALOG (code constant)

Module `apps/api/.../schemas/voice_catalog.py`. A curated allow-list; not a DB table; not snapshotted.

| Field | Type | Notes |
|-------|------|-------|
| `cartesia_voice_id` | `str` | The provider voice id; the value stored into `AgentConfig.voice.cartesia_voice_id`. |
| `name` | `str` | Display name. |
| `language` | `str` | ISO-639-1. |
| `gender` | `"masculine"\|"feminine"\|"gender_neutral"\|null` | Optional metadata for filtering. |
| `description` | `str` | Style/character blurb. |
| `tts_model_hint` | `str \| null` | Suggested TTS model for this voice. |
| `deprecated` | `bool = False` | Hidden from new selection; published configs referencing it still load. |

Derived: `VOICE_IDS: frozenset[str]` (membership for save-time validation).
`SAMPLE_PHRASE: str` — a fixed, PHI-free constant used for all sample synthesis (never a parameter).

### 2.2 ModelSpec / MODEL_CATALOG (code constant)

Module `apps/api/.../schemas/model_catalog.py`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `str` | Stored into `AgentConfig.llm.model` or `.stt.model`. |
| `label` | `str` | Display name. |
| `description` | `str` | One-liner. |
| `kind` | `"llm" \| "stt"` | Filters the picker. |
| `provider` | `str` | e.g. "vertex", "cartesia". |
| `deprecated` | `bool = False` | Hidden from new selection; published configs still load. |
| `default` | `bool = False` | Marks the seed default per kind. |

Seeds — LLM: `gemini-3.1-flash-lite` (default), `gemini-2.5-flash`, `gemini-2.5-flash-lite`,
`gemini-2.5-pro` (all Vertex-served). STT: `ink-whisper`. Derived: `LLM_MODEL_NAMES`,
`STT_MODEL_NAMES` frozensets.

### 2.3 CustomVariableReferences (computed response)

`GET /v1/admin/custom-variables/{id}/references` returns referencing locations, computed by scanning
`agent_profiles.draft_config` **and** `agent_profile_versions.config` for the exact `{{name}}` token
across the 8 prompt fields + SMS template bodies.

```
{ "profiles": [ { "id": "<uuid>", "name": "<profile>", "where": ["draft" | "v<N>", "<field>"] } ] }
```

**PHI rule**: names + field/location identifiers only — never prompt text or per-call values.

### 2.4 Variable catalog addition: `contact_name` builtin

Add to `BUILTIN_VARIABLES` (`variable_catalog.py`): `name="contact_name"`, `tier="builtin"`,
`phi=false`, `default="there"`, `description="The contact's full name."`, `example="Margaret Doe"`,
placed adjacent to `elder_name`. Resolves from the **same** `elder.name` source. Mirror additions:
`prompt_vars.py` `BUILTIN_DEFAULTS["contact_name"]="there"`; `builtin_vars.py`
`resolved["contact_name"]=full`. `elder_name` and the legacy `{elder_name}` slot remain forever.

---

## 3. Request/response shape changes (Pydantic)

| Schema | Change |
|--------|--------|
| `DraftUpdate` | + `expected_revision: int \| None = None` (optional; editor always sends it). |
| `ProfileDetail`, `ProfileSummary` | + `draft_revision: int`. |
| `TestLlmRequest` (new) | `{ messages: [{role, content}], sample_vars: dict[str,str], config?: AgentConfig }` — config defaults to the stored draft. |
| `TestLlmResponse` (new) | `{ assistant: str, tool_calls: [{name, args}] }` (stub tool echoes). |
| `TestAudioRequest` (new) | `{ sample_vars: dict[str,str], config?: AgentConfig }`. |
| `TestAudioResponse` (new) | `{ url: str, token: str, room: str }`. |
| `VoiceCatalogResponse` (new) | `{ voices: VoiceSpec[] }`. |
| `ModelCatalogResponse` (new) | `{ models: ModelSpec[] }`. |
| `CustomVariableReferences` (new) | see §2.3. |

---

## 4. Validation rules (where enforced)

| Rule | Enforcement point | Behavior |
|------|-------------------|----------|
| Voice selection ∈ `VOICE_IDS` | handler (`update_draft`/`publish`/`rollback`) | 422 with field-level `loc` `["body","config","voice","cartesia_voice_id"]`; frozen published versions still deserialize on read. |
| LLM model ∈ `LLM_MODEL_NAMES`; STT ∈ `STT_MODEL_NAMES` | handler (same three) | 422 with `loc` `["body","config","llm","model"]` / `[...,"stt","model"]`; frozen versions unaffected. |
| Stale draft | repo guarded UPDATE | `rowcount==0` + row exists → `StaleDraftError` → **409**; row absent → **404**. |
| Custom var name collision | existing Pydantic + DB | builtin → 422; duplicate → 409 (authoritative; client mirrors for instant feedback). |
| Delete custom var still referenced | client `ConfirmDialog` over §2.3 | warn-don't-block (hard delete proceeds after confirm). |
| PHI custom var in SMS body | existing `custom_phi_sms_violations` | 422 (unchanged). |
| Sample synthesis input | structural | only `SAMPLE_PHRASE` constant reaches Cartesia — no contact data path. |
| Test session side effects | agent `session_kind=="test"` branch | no `Call`/wellness/audit rows; no `/v1/tools/*`; no egress; no SIP; no real-PHI lookup. |

---

## 5. Relationships (unchanged)

`Elder.agent_profile_id → AgentProfile.id` (assignment tier). `AgentProfile.is_default_{inbound,outbound}`
partial-unique per direction. `AgentProfileVersion.profile_id → AgentProfile.id`. Config resolution
precedence (existing): per-call override → per-contact assignment → per-direction default → built-in
`DEFAULT_AGENT_CONFIG` (now surfaced read-only in the Defaults UI).
