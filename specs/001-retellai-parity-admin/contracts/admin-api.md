# API Contract: Admin endpoints (new & changed)

**Feature**: `001-retellai-parity-admin` | **Date**: 2026-06-13

All endpoints are under `require_admin_session`; mutations and test runs additionally require
`require_admin_role(ADMIN)` (viewers get 403 — FR-030). Error envelope is the existing `{ "detail": ... }`.
Field-level 422s use a pydantic-style `loc` list so the admin-ui `tryParseFieldErrors` lands them on the
right control. No endpoint returns or logs per-call PHI values.

---

## NEW — Voice catalog

### `GET /v1/admin/voice-catalog`
- **200** `{ "voices": VoiceSpec[] }` where `VoiceSpec = { cartesia_voice_id, name, language, gender, description, tts_model_hint, deprecated }`.
- Read-only; mirrors `GET /v1/admin/tool-catalog`. Backs the `VoiceSection` searchable picker (FR-009).

### `GET /v1/admin/voice-catalog/{voice_id}/sample`
- **200** `audio/mpeg` byte stream — a fixed, PHI-free phrase synthesized via Cartesia `POST /tts/bytes`, cached per `(voice_id, model)` (FR-010).
- **404** if `voice_id ∉ VOICE_IDS`.
- Secret `CARTESIA_API_KEY` stays server-side; under admin session + rate limiting.

---

## NEW — Model catalog

### `GET /v1/admin/model-catalog`
- **200** `{ "models": ModelSpec[] }` where `ModelSpec = { id, label, description, kind: "llm"|"stt", provider, deprecated, default }`.
- Read-only; backs the curated `LLMSection`/`STTSection` selects (FR-011, FR-012).

---

## NEW — Custom-variable references (delete-guard)

### `GET /v1/admin/custom-variables/{variable_id}/references`
- **200** `{ "profiles": [ { "id": "<uuid>", "name": "<profile>", "where": ["draft"|"v<N>", "<field>"] } ] }`.
- Scans `agent_profiles.draft_config` **and** `agent_profile_versions.config` for the exact `{{name}}` token across the 8 prompt fields + SMS bodies (FR-007). Names/locations only — never prompt text or values.
- **404** if the variable does not exist.

---

## NEW — Agent test / simulation

### `POST /v1/admin/profiles/{profile_id}/test/llm`
- **Body** `{ "messages": [{ "role": "user"|"assistant", "content": str }], "sample_vars": { <name>: <str> }, "config"?: AgentConfig }` (omitted `config` → use the stored draft).
- **200** `{ "assistant": str, "tool_calls": [{ "name": str, "args": object }] }`.
- Runs the draft prompt+tools against **Vertex AI** with stub (no-op) tools — no DB writes, no `/v1/tools/*` calls (FR-025, FR-027). `sample_vars` apply to this run only; no real contact PHI is loaded (FR-026).
- **422** if the draft references an unsupported voice/model (same validation as save).
- **403** for viewers.

### `POST /v1/admin/profiles/{profile_id}/test/audio`
- **Body** `{ "sample_vars": { <name>: <str> }, "config"?: AgentConfig }`.
- **200** `{ "url": "<wss livekit url>", "token": "<short-TTL join JWT>", "room": "usan-test-<uuid>" }`.
- Mints a join-only `AccessToken` (`VideoGrants(room_join, can_publish, can_subscribe)`, ~10–15 min) and dispatches the agent in `session_kind="test"` with the draft config + sample vars inline in metadata (FR-028). No PSTN, no phone number, no `Call` row. See [agent-test-session.md](./agent-test-session.md).
- **403** for viewers.

---

## CHANGED — Profile draft (optimistic concurrency)

### `PUT /v1/admin/profiles/{profile_id}/draft`
- **Body** adds optional `expected_revision: int` (`DraftUpdate`). When present and ≠ current `draft_revision`, the guarded UPDATE matches 0 rows → **409** `{ "detail": "This draft was changed by someone else since you opened it. Reload to see the latest, then re-apply your changes." }` (FR-032).
- Omitted `expected_revision` → unconditional save (backward compatible). The editor always sends it.
- Also now runs **voice + model catalog membership** validation alongside the existing PHI-SMS gate → **422** with field-level `loc` for an unsupported `voice.cartesia_voice_id` / `llm.model` / `stt.model` (FR-014).
- **404** vs **409** disambiguation: after a 0-rowcount UPDATE, re-SELECT — row present → 409, absent → 404.

### `GET /v1/admin/profiles/{profile_id}` and `GET /v1/admin/profiles`
- `ProfileDetail` / `ProfileSummary` responses add `draft_revision: int` so the editor loads the token with the draft.

### `POST /v1/admin/profiles/{profile_id}/publish` and `POST .../rollback/{version}`
- Both now also run voice+model catalog validation (matching the existing PHI-SMS re-check) so a withdrawn-model/voice snapshot can't be (re)published cleanly. Both increment `draft_revision`.

---

## UNCHANGED — reused as-is

`POST /v1/admin/custom-variables` (create; builtin collision → 422, duplicate → 409),
`PATCH`/`DELETE /v1/admin/custom-variables/{id}`, `GET /v1/admin/variable-catalog` (now also returns the
new `contact_name` builtin with no endpoint change — FR-024), `GET /v1/admin/tool-catalog`,
`POST .../set-default`, `POST .../archive`. **No `/v1/contacts` route is added**; `/v1/admin/elders`
remains the sole recipient route (FR-022/SC-008). No webhook payload key changes.
