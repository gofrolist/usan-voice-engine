# RetellAI Parity — Phase 4c-1: Chat-Agent CRUD — Design

**Status:** approved-design (pre-spec-review)
**Date:** 2026-06-27
**Program:** RetellAI full-API-parity (`docs/superpowers/specs/2026-06-24-retell-full-parity-program-roadmap.md`)
**Builds on:** Phase 4a (api_chat sessions, MERGED `2a936fa`), Phase 4b-1/4b-2 (SMS chat, MERGED). The voice-agent compat overlay (`create-agent` / `create-retell-llm`, Phases 1a/1b).

## 1. Overview

Phase 4 ("Chat + SMS") decomposed into 4a (chat sessions) / 4b (SMS) / **4c** (chat-agent management + analysis). 4c further splits:

- **4c-1 (this spec):** chat-agent CRUD — the 7 `*-chat-agent` operations, served by overlaying the existing `agent_profiles` table with a new `channel` discriminator.
- **4c-2 (separate spec/plan/PR):** `rerun-chat-analysis` + the Vertex-backed `chat_analysis` pipeline. **Out of scope here.**

**Goal:** move the 7 chat-agent ops from documented-501 to served, conformant to the pinned oracle + `retell-sdk==5.53.0`, with `KNOWN_GAPS` staying `frozenset()`. A RetellAI client that creates and manages chat agents repoints to us with zero changes.

**Inert until deployed:** ships behind a `v*` tag (migration `0045`); no new env keys; the surface is reachable only once a super-admin mints a compat key. Merge to `main` is inert.

## 2. Scope — the 7 operations (oracle, info.version 3.0.0)

| operationId | method | path | success | response |
|---|---|---|---|---|
| createChatAgent | POST | `/create-chat-agent` | 201 | `ChatAgentResponse` |
| getChatAgent | GET | `/get-chat-agent/{agent_id}` | 200 | `ChatAgentResponse` |
| getChatAgentVersions | GET | `/get-chat-agent-versions/{agent_id}` | 200 | `ChatAgentResponse[]` |
| listChatAgents | GET | `/list-chat-agents` | 200 | `ChatAgentResponse[]` (deprecated) |
| updateChatAgent | PATCH | `/update-chat-agent/{agent_id}` | 200 | `ChatAgentResponse` |
| deleteChatAgent | DELETE | `/delete-chat-agent/{agent_id}` | 204 | (none) |
| publishChatAgent | POST | `/publish-chat-agent/{agent_id}` | 200 | (no body, deprecated) |

All 7 are currently 501 stubs in `compat/routers/unsupported.py:45-52`. Moving an op to served = **remove its `_UNSUPPORTED` entry** + add a real route at the oracle's **exact** path (`test_501_stub_paths_match_oracle_exactly` enforces exact param names). `rerun-chat-analysis` (`unsupported.py:81`) stays 501 (→ 4c-2).

### 2.1 Schemas (verbatim from oracle/SDK)

- **`ChatAgentRequest`** — `type:object`, no own `required` list. `create-chat-agent` overlays `required:[response_engine]`; `update-chat-agent` uses the bare request (everything optional → partial update). Fields: `response_engine`, `agent_name`, `version_title`, `auto_close_message`, `end_chat_after_silence_ms` (min 120000 / max 259200000 / default 3600000), `language`, `webhook_url`, `webhook_events`, `webhook_timeout_ms`, `data_storage_setting`, `data_storage_retention_days`, `opt_in_signed_url`, `signed_url_expiration_ms`, `post_chat_analysis_data`, `post_chat_analysis_model`, `pii_config`, `guardrail_config`, `handbook_config`, `timezone`.
- **`response_engine`** = oneOf [`ResponseEngineRetellLm` {`llm_id`, `type:'retell-llm'`, `version?`} | `ResponseEngineCustomLm` {`llm_websocket_url`, `type:'custom-llm'`} | `ResponseEngineConversationFlow` {`conversation_flow_id`, `type:'conversation-flow'`, `version?`}]. No formal `discriminator`; the `type` string tags it. **Only `retell-llm` is honored** (we are the LLM); `custom-llm`/`conversation-flow` → 422 (mirrors the voice overlay).
- **`ChatAgentResponse`** = allOf of (A) {`agent_id` **required**, `version:int`, `base_version:int|null`, `assigned_tags:str[]`, `is_published:bool`} + (B) `ChatAgentRequest` with `response_engine` **required** + (C) {`last_modification_timestamp` **required**, ms}. **Net required: `agent_id`, `response_engine`, `last_modification_timestamp`.** `version`/`base_version`/`assigned_tags`/`is_published` are present-but-not-required.
- **Round-trip targets:** `assert_conforms(payload, "ChatAgentResponse")` + `assert_sdk_roundtrip(payload, "retell.types:ChatAgentResponse")`. The list / versions SDK aliases are `List[ChatAgentResponse]` (TypeAliases, no `.model_validate`) → round-trip each **item** against the concrete `ChatAgentResponse`, exactly as the list-agents test does.

## 3. Backing model — overlay `agent_profiles` + a `channel` discriminator

A RetellAI chat-agent is **not** a new entity in our schema: it is an `agent_profiles` row, exactly as a voice agent is (`AgentProfile` IS the agent AND its retell-llm — `agent_id`/`llm_id` are two prefixed views of the same UUID via `compat/ids.py`). It reuses the `agent_<hex>` id space (the path param is literally `agent_id`), the versioning machinery (`published_version`, `agent_profile_versions`), RLS, and the id-codec. A chat session created via `create-chat` already FKs `agent_profiles` (`ChatSession.agent_profile_id`), so nothing about 4a/4b changes.

**The two-step flow (mirrors voice):** `create-retell-llm` creates the profile (the "llm half", holds the prompt); `create-chat-agent` carries `response_engine.llm_id` (decodes to the **same** profile UUID), binds the chat config, marks `channel='chat'`, and publishes. `ChatAgentRequest` has **no** `general_prompt`/`begin_message` — the prompt lives in the LLM half. `create-chat-agent` requires a resolvable `response_engine.type=='retell-llm'` `llm_id` (else 422/404).

### 3.1 Migration 0045 (additive, owner-DDL, inert)

```
ALTER TABLE agent_profiles ADD COLUMN channel TEXT NOT NULL DEFAULT 'voice';
```

- `revision='0045'`, `down_revision='0044'`. Additive column on the existing `agent_profiles` table → **inherits the table's existing `usan_app` GRANT + RLS policy** (no `_enable_rls`, like 0043/0044). Runs as the `usan` owner on deploy.
- `server_default='voice'` backfills every existing row to `'voice'` — so all current voice agents stay voice and every pre-channel caller stays correct.
- ORM: `AgentProfile.channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'voice'"))`.
- Adding a full-row column breaks no existing query: all readers either `db.get(AgentProfile, ...)` or `select(AgentProfile)` (carry the new attribute) or project explicit columns (unaffected). Verified by the reader audit (§4).
- `downgrade()` drops the column. No new index is required (the leak filters are low-cardinality predicates on a per-org table; a partial index is optional and omitted — YAGNI).

Channel values are the string literals `'voice'` / `'chat'`; repo/bridge signatures type them as `Literal["voice", "chat"]`.

## 4. The leak audit — sealing the voice/chat collision (the Phase-3 lesson, applied up front)

A new discriminator on a **shared** table re-creates the Phase-3 risk: a chat row is `ACTIVE`+`published` just like a voice row, and `agent_id`/`llm_id` are two views of the same UUID, so **every** voice/admin reader leaks chat rows and every call-plane selector could dial one — unless filtered. This audit (from a 4-reader sweep) enumerates every vector and its fix. The plan dedicates one task to landing all of these **together with** the column, so there is never a window where a chat row can leak.

**Key refinement — retell-llm ops stay channel-agnostic.** A Retell-LLM is channel-neutral shared infra; the chat two-step needs `get-retell-llm`/`list-retell-llms` to keep working after `create-chat-agent` flips the row to `channel='chat'`. So the cross-resource guard applies **only to agent-typed ops** (`get-agent`=voice, `get-chat-agent`=chat); the retell-llm ops carry **no** channel filter. Likewise agent-name uniqueness (`uq_agent_profiles_name_org`) is org-wide regardless of channel.

### 4.1 Compat surface — two choke points

- **`agent_bridge.list_agent_profiles`** (`agent_bridge.py:325-328`) — the single list reader behind `GET /list-agents`, `POST /v2/list-agents`, **and** `GET /list-retell-llms`. **Add a `channel: str | None` param.** `list-agents`/`v2/list-agents` pass `'voice'`; `list-retell-llms` passes `None` (all). `list-chat-agents` (new) passes `'chat'`. `v2/list-agents` keeps its existing `filter_criteria.channel=='chat' → []` short-circuit but now also base-filters to voice so the default / `channel=='voice'` cases exclude chat rows.
- **`agent_bridge._load_active`** (`agent_bridge.py:124-128`) — the single single-row gate behind get/update/delete/publish-agent, get-agent-versions, delete-agent-version, **and** every retell-llm op (via `get_llm_profile`). **Add an `expected_channel: str | None` param**: after the archived check, `if expected_channel is not None and profile.channel != expected_channel: raise CompatError(404, ...)`. Agent ops pass `'voice'`; chat-agent ops pass `'chat'`; retell-llm ops pass `None`. This one guard blocks both the read leak and every mutation against a wrong-channel row.
- `agent_bridge._unique_name` (`agent_bridge.py:109`) → `list_profiles` with **no** channel (org-wide name uniqueness).
- `create-agent` (`agent_bridge.bind_agent`) explicitly stamps `channel='voice'` on the row it publishes (belt-and-suspenders over the server default).

### 4.2 Native / admin-ui surface

- **`agent_profiles_repo.list_profiles`** (`repositories/agent_profiles.py:72`) — the single enumerate behind `GET /v1/admin/profiles` (`routers/admin_profiles.py:44-58`), which feeds the admin-ui Agents list **and every profile-picker dropdown** (Contacts, Schedules, CallNow). **Add a `channel: Literal["voice","chat"] | None` param;** `admin_profiles.list_profiles` passes `'voice'`. This one change scopes the whole native voice surface.
- `routers/admin_profiles.py` `get_profile` (`GET /v1/admin/profiles/{id}`) → add a `channel != 'voice' → 404` guard (defense-in-depth; by-id, not enumerated).
- `repositories/agent_profiles.create_profile` (`:40-65`, native voice create) → stamp `channel='voice'` explicitly on insert.

### 4.3 Call-plane selectors (a chat agent must never be dialed / defaulted / assigned / used as an override)

- **`is_live_profile`** (`repositories/agent_profiles.py:570`) — the liveness gate behind every `profile_override`. **Add a `channel: Literal["voice","chat"] | None = None` param**; require `profile.channel == channel` when set. Voice callers pass `'voice'`: `services/outbound_calls.require_live_override` (`:33`, the shared gate for POST /v1/calls, admin call-now, schedules, batches), `compat/call_create.py` (`:148/213/255`), `compat/batch_create.py:170`, `routers/batches.py:97`. → a chat agent passed as a voice override 422s with the standard `OVERRIDE_ERROR`.
- **`set_default`** (`repositories/agent_profiles.py:256`) — the **only** writer of `is_default_inbound`/`is_default_outbound` (voice-only flags). **Reject `channel != 'voice'`** (422/`ProfileInUseError`). A chat agent can never become a dial default.
- **`get_default_profile`** (`:375`) + **`get_default_holder`** (`:394`) — add `.where(AgentProfile.channel == 'voice')` (one-line, defense-in-depth alongside the `set_default` write guard). Seals inbound resolution (`runtime.py:48`) and the compat outbound default fallback (`call_create.py:157`).
- **`_resolved_from_profile`** (`:422/430`, inside `resolve_agent_config`/`resolve_call_policy`) — return `None` for `channel != 'voice'`, so an override/contact tier mis-pointed at a chat profile **falls through** to the next tier instead of driving a phone call. Seals the runtime config-resolution plane (`runtime.py:48`, `tools.py:330`, `schedule_orchestrator.py`, `livekit_dispatch.py:586`).
- **`contacts.assign_profile`** (`repositories/contacts.py:92`) — validate the target is `channel='voice'` (reject 400/422) before binding, so an admin can't assign a chat agent to a contact the scheduler then dials.

### 4.4 Create-side invariants (the safest seal)

`create-chat-agent` **MUST** set `channel='chat'` and **MUST NOT** set `is_default_outbound`/`is_default_inbound` (never call `set_default`) — keeping `create_profile`'s default-false behavior. Combined with §4.3, a chat row can never enter the voice call plane at write time *or* read time.

### 4.5 Confirmed safe, no change

`schedule_orchestrator` materialize paths (propagate a pre-validated `profile_override`), `livekit_dispatch` dispatch (selects no profile), `compat/lifecycle._resolve_agent_profile_id` + `call_serializer._resolve_agent` (serialize-only reads), `phone_numbers._resolve_binding_agents`, `get_version`/`list_versions`/`get_published_config` (profile-id-scoped). All safe **because** §4.3/§4.4 keep chat rows out of the selectors that feed them.

### 4.6 Deferred (documented deviation) — chat-session create stays lenient

The 4a/4b **chat-session** ops (`create-chat`, `create-sms-chat`) resolve `agent_id` via `chat_service` **without** asserting `channel='chat'` (`chat_service.py:64/95/112/172`). Tightening them to require `channel='chat'` would change already-merged 4a/4b behavior and re-seed their tests. 4c-1 leaves them lenient (a chat session may open against a voice agent_id) — this is **not** a safety/PHI leak (same-org, RLS-scoped; a voice profile merely drives a text turn). Recorded as a deviation; a later chat-conformance pass (or 4c-2) may tighten it. Note the resulting, intentional asymmetry: `get-chat-agent(voice_id)` → 404 (strict), but `create-chat(voice_id)` → works (lenient).

## 5. Serialization — `serialize_chat_agent(profile) -> dict`

New `compat/chat_agent_bridge.py` (sibling to `agent_bridge.py`; reuses its helpers). `serialize_chat_agent` builds a `ChatAgentResponse` dict:

- **Required:** `agent_id = encode_agent_id(profile.id)`; `response_engine = {"type": "retell-llm", "llm_id": encode_llm_id(profile.id), "version": <published_version or 0>}`; `last_modification_timestamp = to_ms(profile.updated_at)`.
- **Version fields:** `version = profile.published_version or 0`; `is_published = profile.published_version is not None`. `base_version` and `assigned_tags` omitted (optional; we track neither) → `None` → dropped by `exclude_none`.
- **Echoed chat config:** the submitted `ChatAgentRequest` is stored verbatim in `draft_config["compat_extras"]["chat_agent"]` and echoed back (model `extra='allow'`), exactly as the voice overlay echoes `compat_extras["agent"]`. Fields: `agent_name` (defaults to `profile.name`), `auto_close_message`, `end_chat_after_silence_ms`, `language`, `webhook_*`, `data_storage_*`, `opt_in_signed_url`, `signed_url_expiration_ms`, `post_chat_analysis_data`, `post_chat_analysis_model`, `pii_config`, `guardrail_config`, `handbook_config`, `timezone`, `version_title`. **Persisted-not-honored** (no runtime behavior; `post_chat_analysis_data`/`post_chat_analysis_model` will be consumed by 4c-2's rerun).
- Routes return `serialize_chat_agent(...).model_dump(exclude_none=True)` (the omit-when-null discipline — serializing a null forbidden/optional key fails oracle conformance).

Schemas (`compat/schemas/chat_agents.py`, `ConfigDict(extra='allow')`): `ChatAgentCreateRequest` (`response_engine` required), `ChatAgentUpdateRequest` (all optional), `ChatAgentResponse` echo model — mirror `compat/schemas/agents.py`.

## 6. Operation semantics (mirror the served voice agent router)

- **create-chat-agent** → decode `response_engine.llm_id` → load that profile (channel-agnostic load, then bind) → store `compat_extras["chat_agent"]` → set `channel='chat'` → publish → **201** `ChatAgentResponse`. 422 on non-`retell-llm` engine or unresolvable `llm_id`; 404 if the llm profile doesn't exist in-org. Never sets default flags. Validate the overlay through native `AgentConfig` before persist (as voice does).
- **get-chat-agent** → `_load_active(expected_channel='chat')` (→ 404 on a voice id) → serialize. `version` query (`AgentVersionReference`) accept-and-ignore (return the published view) — documented deviation.
- **get-chat-agent-versions** → `_load_active(expected_channel='chat')` + `list_versions` → `ChatAgentResponse[]` (serialize each version, mirroring voice `get-agent-versions`).
- **list-chat-agents** → `list_agent_profiles(channel='chat')`, keyset cursor over `(name, id)`, honor `limit`, **bare array** (deprecated). `is_latest`/`pagination_key_version` accept-and-return-current-view (we surface one published view per profile) — documented deviation.
- **update-chat-agent** → `_load_active(expected_channel='chat')` → merge the partial `ChatAgentUpdateRequest` into `compat_extras["chat_agent"]` (merge-not-replace) → re-publish → **200** `ChatAgentResponse`.
- **delete-chat-agent** → `_load_active(expected_channel='chat')` → archive (`status=ARCHIVED`) → **204** (soft-delete, mirrors voice `delete-agent`).
- **publish-chat-agent** → `_load_active(expected_channel='chat')` → thin publish of the latest → **200 no body** (deprecated; mirrors voice `publish-agent`).

All writes commit explicitly (`get_compat_db` does not autocommit). Each handler calls the PHI-free `_audit(request, op, agent_id)` (org id + op + agent id only; pattern from `agents.py:39-41`). Auth via `Depends(get_compat_db)` (compat-key Bearer → RLS-scoped org).

## 7. Components / files

- **Migration:** `apps/api/migrations/versions/0045_agent_channel.py` (additive column).
- **ORM:** `AgentProfile.channel` in `db/models.py`.
- **Repo:** `repositories/agent_profiles.py` — `list_profiles(channel=...)`, `is_live_profile(channel=...)`, `get_default_profile`/`get_default_holder` voice filter, `_resolved_from_profile` voice guard, `set_default` voice guard, `create_profile` stamp voice; `repositories/contacts.assign_profile` voice guard.
- **Compat bridge:** new `compat/chat_agent_bridge.py` (`serialize_chat_agent`, `create_chat_agent`, `update_chat_agent`, `delete_chat_agent`, `publish_chat_agent`, `list_chat_agents`, `get_chat_agent`, `get_chat_agent_versions`); `agent_bridge.list_agent_profiles(channel=...)` + `_load_active(expected_channel=...)` + `bind_agent` stamps voice.
- **Compat schemas:** new `compat/schemas/chat_agents.py`.
- **Compat router:** new `compat/routers/chat_agents.py`, registered in `compat/app.py`; remove the 7 chat-agent entries from `routers/unsupported.py`.
- **Native:** `routers/admin_profiles.py` passes `channel='voice'` to `list_profiles` + the `get_profile` 404 guard; `services/outbound_calls.require_live_override` + `compat/call_create.py` + `compat/batch_create.py` + `routers/batches.py` pass `channel='voice'` to `is_live_profile`.
- **Conformance:** add `ChatAgentResponse` to the `conformance.py` name map (doc header).
- **Tests:** `tests/compat/test_freeze_chat_agents.py` (`@pytest.mark.frozen`); behavior + leak-regression + cross-resource-isolation tests (see §8); `docs/deployment/chat-agents.md`.

## 8. Testing strategy

- **Frozen conformance** (`test_freeze_chat_agents.py`, `pytestmark = pytest.mark.frozen`): create over real HTTP via `compat_client`/`compat_headers` → `assert_conforms(payload, "ChatAgentResponse")` + `assert_sdk_roundtrip(payload, "retell.types:ChatAgentResponse")` for create/get/list-item/version-item. Assert `exclude_none` omission (e.g. `base_version`/`assigned_tags` absent when unset).
- **Behavior:** create→get round-trip (echoed config returns verbatim); update partial-merge; delete→archive→404; publish 200-no-body; list-chat-agents returns only chat agents; non-`retell-llm` engine → 422; unresolvable `llm_id` → 422/404.
- **Cross-resource isolation:** `get-agent(chat_id)` → 404; `get-chat-agent(voice_id)` → 404; `update/delete-agent(chat_id)` → 404; `get-retell-llm(chat-bound llm_id)` → **200** (channel-agnostic — regression-guards the retell-llm refinement).
- **Leak regression (the crux):** seed a voice + a chat agent; assert voice `list-agents` and `v2/list-agents` and `GET /v1/admin/profiles` exclude the chat agent; assert `is_live_profile(chat_id, channel='voice')` is False and a chat agent passed as a voice `profile_override` 422s; assert `set_default(chat_id)` and `contacts.assign_profile(chat_id)` reject; assert `list-retell-llms` **includes** the chat-bound profile (agnostic).
- **Surface coverage:** `test_surface_coverage.py` stays green automatically once the routes exist and the 7 stubs are removed (served routes land in `app.routes`); `KNOWN_GAPS` stays `frozenset()`. `test_compat_fidelity.py`'s 501 parametrize list does not include chat-agent paths → no edit.
- **Gate:** `ruff check . && ruff format --check .`, `uv run mypy` (config `files=["src"]`; never `mypy .`), `uv run pytest` (`-n auto`). Run all locally before pushing.

## 9. Posture / deviations (4c-1)

1. Only `response_engine.type=='retell-llm'` honored; `custom-llm`/`conversation-flow` → 422.
2. `channel` discriminator added to `agent_profiles`; all voice/admin/call-plane readers filtered/guarded (§4); retell-llm ops intentionally channel-agnostic.
3. Chat config (`auto_close_message`, `end_chat_after_silence_ms`, `post_chat_analysis_data`, `post_chat_analysis_model`, `pii_config`, `guardrail_config`, `handbook_config`, `data_storage_*`, `webhook_*`, `language`, `timezone`, `version_title`) echoed verbatim, **persisted-not-honored**.
4. `version` query (`AgentVersionReference`) accept-and-return-published-view; `base_version`/`assigned_tags` omitted; `is_latest`/`pagination_key_version` accept-and-ignore.
5. Writes always publish; delete = archive; publish = thin (mirror voice).
6. Chat-session create (`create-chat`/`create-sms-chat`, 4a/4b) left lenient — not tightened to require `channel='chat'` (§4.6).
7. Inert: migration `0045` + a `v*` tag; no new env keys; reachable only via a minted compat key. `KNOWN_GAPS` stays `frozenset()`.

## 10. Out of scope → Phase 4c-2

`rerun-chat-analysis` (`PUT /rerun-chat-analysis/{chat_id}` → 201 `ChatResponse`) stays 501. The Vertex-backed `chat_analysis` pipeline, `CompatChat.chat_analysis`, and analysis storage are 4c-2.

## 11. Global constraints (for the plan)

- Commit `feat(api): …`; scope `api`. Squash-merge to protected `main` **only on explicit go-ahead**. **No `v*` tag.** Attribution disabled (no `Co-Authored-By` / footer).
- `apps/api` and `services/agent` never import each other.
- Migration `0045` is owner-DDL (runs as `usan` owner on deploy), additive, inert; single alembic head after = `0045`.
- PHI/secret-safe logging only (`_audit` = org id + op + agent id; never numbers/config/prompt text). `organization_id` is server-set by RLS, never by app code.
- `KNOWN_GAPS` stays `frozenset()`; the 7 served paths use the oracle's exact path strings.
- `exclude_none` discipline on every serialized response.
