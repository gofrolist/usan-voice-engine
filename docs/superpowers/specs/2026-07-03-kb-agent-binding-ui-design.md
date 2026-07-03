# Knowledge Base ↔ agent binding — editor UI (v1)

**Date:** 2026-07-03
**Status:** Design (approved for planning)
**Depends on:** the native knowledge-bases feature (v0.15.0, `/v1/admin/knowledge-bases`, `KbSummary`) and the existing agent profile editor (`apps/admin-ui/src/features/editor`).

## Problem

A knowledge base only powers retrieval (RAG) when the agent's resolved config has a
non-empty `llm.knowledge_base_ids`. Today there is **no admin-UI control to set that** —
binding is only reachable through the RetellAI-compat `PATCH /update-retell-llm` API. An org
admin cannot attach a KB to an agent from the console (they had to have it done via the compat
API). This feature adds a **Knowledge Base section to the agent editor** so binding is
self-service, mirroring RetellAI's agent-editor "Knowledge Base" panel.

There is also a latent bug this feature fixes (see "Silent-strip fix").

## Goals

- An org admin can add/remove the org's knowledge bases for an agent, from the editor.
- Binding is a **draft edit**: staged in the draft and goes live on **Publish**, exactly like
  the prompt, voice, and tools — no separate flow, no auto-publish.
- A binding set via the compat API is visible in the editor and is **never silently dropped**
  when the profile is edited and re-published.

## Non-goals (v1)

- **Per-agent retrieval tuning** (top-K / similarity threshold). These stay on the global
  env defaults (`KB_RETRIEVAL_TOP_K`, `KB_RETRIEVAL_MAX_DISTANCE`). Moving them into per-agent
  config is a separate, larger change.
- **Per-agent KB instruction** string (RetellAI's "Configure Knowledge Base Instruction").
- A dedicated native "bind" endpoint — binding rides the existing draft/publish path.
- Changing the runtime retrieval path, the token encoding, or the compat surface.

## Background: how binding works today

- Storage: `AgentConfig.llm.knowledge_base_ids: list[str] | None` — a list of **encoded**
  `knowledge_base_<hex>` tokens, living in the JSONB `agent_profiles.draft_config` (frozen into
  `agent_profile_versions.config` on publish).
- Encoding: `usan_api.compat.ids.encode_kb_id(uuid) -> "knowledge_base_<hex>"` /
  `decode_kb_id(token) -> uuid` (`apps/api/src/usan_api/compat/ids.py:70-75`).
- Runtime: chat (`compat/chat_service.py`) and voice (`routers/tools.py` `/v1/tools/retrieve_kb_context`)
  both read `cfg.llm.knowledge_base_ids`, decode, and search. **Unchanged by this feature.**
- The native KB list (`GET /v1/admin/knowledge-bases`, `KbSummary`) returns **raw UUIDs**, not
  the encoded tokens the config stores — so the UI cannot map config tokens to KBs without help.

## Silent-strip fix (why the frontend schema change is mandatory)

The editor form is react-hook-form + zod (`apps/admin-ui/src/config/agentConfigSchema.ts`,
`agentConfigSchema` / `llmSchema`). On save it sends the **zod-validated** config to
`PUT /v1/admin/profiles/{id}/draft`. zod objects **strip unknown keys by default**. `llmSchema`
currently declares only `model` and `temperature`, so any `knowledge_base_ids` present in the
loaded `draft_config` is **stripped out of the validated payload on the next save/publish** —
i.e. editing and publishing a profile today would **wipe a compat-set binding**. Adding
`knowledge_base_ids` to `llmSchema` (and the `LLMConfig` type) both enables this feature and
closes that data-loss bug.

## Architecture

### Backend (one field)

Add `agent_ref: str` to `KbSummary` (`apps/api/src/usan_api/schemas/admin_knowledge_bases.py`)
— the encoded token `ids.encode_kb_id(kb.id)`. The `list_knowledge_bases` router populates it
from the existing `kb.id`. This is the single reconciliation point: the UI now receives, per
KB, both the raw `id` (for its own CRUD) and the `agent_ref` token (what the config stores).
No other backend change; the KB detail endpoint and the retrieval path are untouched.

Rationale for choosing "expose the token" over the alternatives:
- **Rejected — new native bind endpoint** (`PUT .../profiles/{id}/knowledge-bases`): duplicates
  the draft/publish path, and binding-as-immediate-action was explicitly rejected (binding is a
  draft edit). More surface for no gain.
- **Rejected — store raw UUIDs in the config:** the runtime + compat decode encoded tokens;
  changing the stored form is a broad, cross-surface change touching live retrieval.

### Frontend

**Type + schema (`apps/admin-ui/src/config/agentConfigSchema.ts`, `src/types/api.ts`):**
- `LLMConfig` gains `knowledge_base_ids: string[]` (tokens).
- `llmSchema` gains `knowledge_base_ids: z.array(z.string()).default([])` so it round-trips
  through load → validate → save (fixes the strip bug). Default `[]` keeps profiles that never
  had the key valid.

**New KB types + hook (reuse the existing `features/knowledgeBases`):**
- `KbSummary` (admin-ui type) gains `agent_ref: string`.
- The editor's KB section uses the existing `useKnowledgeBases()` list hook (now returning
  `agent_ref`) to populate the picker. No new endpoint call.

**New section `KnowledgeBaseSection` (`src/features/editor/sections/KnowledgeBaseSection.tsx`):**
- Reads/writes `form` field `llm.knowledge_base_ids` (array of tokens), like other sections take
  `{ form }`.
- Renders the org's KBs (name + status badge from `useKnowledgeBases()`), each with a
  bound/unbound toggle (checkbox or add/remove). Toggling updates the form array
  (`form.setValue("llm.knowledge_base_ids", …, { shouldDirty: true })`).
- **Unknown-token safety:** a token in `llm.knowledge_base_ids` with no matching KB in the list
  (deleted KB, or a compat-created KB the list doesn't surface) renders as a disabled
  "unknown knowledge base (<token>)" row and is **retained** in the array on every edit — never
  dropped. Removing it is possible but explicit.
- Empty state: "No knowledge bases yet — create one under Knowledge" (link to `/knowledge-bases`).

**Editor wiring:**
- Add `"knowledge_base"` to the `SectionKey` union and `SECTION_LABELS` in
  `src/config/fieldMeta.ts` (label e.g. "Knowledge").
- Add it to `SECTION_ORDER` and the section-render switch in `ProfileEditorPage.tsx` (adjacent to
  `llm`), and a section summary (e.g. "2 knowledge bases" / "None") for `SectionRail`.

Admin gating is inherited — the profile editor is already admin-only.

### Data flow

```
load draft_config → form (incl. llm.knowledge_base_ids tokens)
KnowledgeBaseSection: useKnowledgeBases() → [{id, name, status, agent_ref}]
  → map config tokens ↔ agent_ref for display; admin toggles → form.setValue(llm.knowledge_base_ids)
Publish → POST /v1/admin/profiles/{id}/publish (existing) → agent_profile_versions.config frozen
runtime (unchanged) resolves published cfg.llm.knowledge_base_ids → retrieval searches those KBs
```

## Error handling

- KB-list fetch fails → the section shows an inline error; **already-bound tokens still render**
  from the config (as tokens / unknown rows), so a list outage can't hide or drop a binding.
- Unknown/orphaned token → shown, retained, removable — never silently dropped.
- Invalid state is impossible to submit: the field is `string[]`; the server re-validates the
  full `AgentConfig` on draft PUT and publish (existing behavior).
- Concurrency (409 draft conflict) and publish errors are handled by the existing editor flow;
  the KB field is just another config field within it.

## Testing

Backend (`apps/api/tests/`):
- `list_knowledge_bases` returns `agent_ref == ids.encode_kb_id(id)` for each KB, and it
  round-trips (`decode_kb_id(agent_ref) == id`).

Frontend (`apps/admin-ui/src/test/`):
- `KnowledgeBaseSection`: renders bound vs available from a mocked list + form value; toggling a
  KB updates `llm.knowledge_base_ids`; an unknown token renders and is preserved after another
  edit.
- **Strip-regression guard:** a profile whose `draft_config.llm.knowledge_base_ids` is non-empty
  survives a load → (edit an unrelated field) → serialize round-trip with the tokens intact
  (schema-level test against `agentConfigSchema`, and/or an editor save test asserting the PUT
  body still carries the tokens).
- `agentConfigSchema` test: `llm.knowledge_base_ids` defaults to `[]` and is preserved, not
  stripped.

Coverage target 80%+; follow existing editor-section + api test patterns.

## Rollout

- No migration (the field already exists in `AgentConfig`; only the frontend schema + one
  response field change). Ships behind the existing admin auth; visible after the next `v*` tag
  deploy. No new env keys.
- After deploy, the Clara/Sales binding set today via compat is visible in the editor and safe to
  edit.
