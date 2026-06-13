# Quickstart & Validation: RetellAI-Parity Admin Console & Agent Studio

**Feature**: `001-retellai-parity-admin` | **Date**: 2026-06-13

Runnable validation scenarios that prove the feature end-to-end. Each maps to spec success criteria
(SC-xxx) and functional requirements (FR-xxx). See [contracts/](./contracts/) and
[data-model.md](./data-model.md) for shapes; this guide is run/validate only — no implementation code.

## Prerequisites

- Local stack up: `make up` (builds `usan-agent-base:local` if missing, then compose up).
- API tests: `cd apps/api && uv sync && uv run pytest -v && ruff check . && uv run mypy`.
- Agent tests: `cd services/agent && uv sync && uv run pytest -v && ruff check .`.
- Admin-ui: `cd apps/admin-ui && npm ci && npm test && npm run build`.
- New env present (local `.env` and, before any deploy tag, the VM `.env`): `CARTESIA_API_KEY`,
  `GCP_PROJECT`, `VERTEX_LOCATION` (the API needs ADC with `aiplatform.user` for the text-test path).
- Signed in to the admin console as an **admin** (a second admin login is handy for the concurrency test).

## Scenario 1 — Inline variable declaration (US1 → SC-001, SC-002, FR-001–FR-008)

1. Open a profile → System prompt. Type `{{state}}` and `{{med_name}}`.
2. Expect each to be flagged undeclared with a per-token **Declare** action (no navigation).
3. Click **Declare** on `{{state}}`; fill description/example/PHI; confirm.
4. **Expected**: the warning for `{{state}}` clears immediately (catalog refetch); the token renders as known; no page change occurred.
5. Use **Declare all remaining** for `{{med_name}}`; confirm it clears too.
6. Open the variable palette → built-ins and customs are grouped; PHI entries show a **PHI** badge; inserting one drops `{{name}}` at the cursor.
7. Try to declare a custom named `elder_name` → blocked with a builtin-collision message (FR-006).
8. Go to Variables → delete a referenced custom → the confirm dialog lists the referencing profiles/locations before deleting (FR-007).
- **Pass**: zero navigations to declare; warnings self-clear; collision blocked; delete-guard lists references.

## Scenario 2 — Voice / model pickers with preview (US2 → SC-003, SC-004, FR-009–FR-015)

1. Profile → Voice. The voice field is a **searchable list** (not free text) with language/gender/style; no identifier typing.
2. Click **▶ play** on a voice → a short sample plays in the browser within ~30s (first play synthesizes; later plays are cached). **Pass SC-003.**
3. Profile → LLM and STT are **curated selects**; pick `gemini-2.5-flash` and `ink-whisper`.
4. Save & publish; run an audio test (Scenario 4) → the agent uses the selected voice/models (FR-015).
5. Negative: via API, `PUT .../draft` with `voice.cartesia_voice_id="not-a-real-voice"` → **422** with field-level `loc`. A previously-published version that referenced a now-deprecated id still loads for viewing (FR-014). **Pass SC-004.**

## Scenario 3 — Understandable & editable defaults (US3 → SC-005, SC-006, FR-016–FR-020)

1. Open **Defaults**. Read the per-direction statement of the current default profile and the plain-language resolution order (override → contact assignment → per-direction default → built-in fallback); the built-in fallback is shown **read-only**.
2. Set inbound and outbound default profiles (only active+published selectable). **Pass SC-005** (you can state what runs for an unassigned call of each direction).
3. Follow the **edit** link to the chosen default profile, change the greeting, publish.
4. Trigger an unassigned outbound call (or audio test using the outbound default) → the new greeting is used. **Pass SC-006.**
5. Archive the default profile → Defaults surfaces that the default is no longer effective and prompts for a replacement (FR-020).

## Scenario 4 — Test before publish (US5 → SC-009, FR-025–FR-028)

1. Open a profile draft → **Test LLM**. Provide `sample_vars` (e.g. `contact_name=Margaret`). Exchange a few turns; if the model calls a tool, the call+args are echoed and a stubbed result returns (no real action).
2. Open **Test Audio** → a browser webcall connects; you hear the selected voice and can speak for a short bounded session. No phone rings; no phone number is consumed.
3. After both tests, query the DB / `GET /v1/admin/calls` → **no** new `Call`, wellness, medication, or audit row was created. **Pass SC-009 / FR-027.**
4. Confirm no real contact PHI appeared — only the synthetic `sample_vars` (FR-026).

## Scenario 5 — Generic naming (US4 → SC-007, SC-008, FR-021–FR-024)

1. Walk every admin screen (nav, Contacts page, Calls, Queues, Profiles, Defaults help text, column headers, empty states). **Expected**: no user-facing "elder/elders"; the term is "Contact/Contacts"; the route is `/contacts`. **Pass SC-007.**
2. In the prompt editor, type `{{contact_name}}` → recognized immediately (no "unknown variable"); it appears in the palette (FR-024).
3. Backward compat: existing external integration calls (`/v1/admin/elders`, webhook consumers expecting `elder_id`) still succeed; a published version embedding `{{elder_name}}` still renders identically. **Pass SC-008 / FR-022, FR-023.**
4. QA note: a grep for the domain word "elder" must exclude the intentionally-retained token identifiers `elder_name` / `{elder_name}` (frozen, kept on purpose).

## Scenario 6 — Optimistic concurrency (FR-032 → SC-011)

1. Open the same profile draft in two admin sessions (A and B).
2. In A: edit and **Save** (succeeds; `draft_revision` advances).
3. In B (still on the old revision): edit and **Save** → **409** with a reload-warning banner; B's edits are not silently lost.
4. In B: **Reload** (re-fetches the latest draft + revision), re-apply, Save → succeeds. **Pass SC-011** (zero silent lost updates).

## Regression gates (Constitution)

- `apps/api` and `services/agent` still do not import each other (Service Isolation).
- `test_legacy_config_still_deserializes` passes — older frozen versions referencing now-withdrawn voices/models still load (forward-compat).
- A contract test asserts the api-side prompt substitutor matches the agent's `prompt_vars.substitute` on a shared corpus; a cross-layer test asserts `contact_name` resolves identically to `elder_name` in both mirrors.
- mypy + ruff green in CI; admin-ui Vitest green (including the Monaco textarea-fallback path for the inline-declare chips).
