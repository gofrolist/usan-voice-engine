# Phase 1 Spec — Activate, Validate, Freeze & Harden the Core Calling Surface

**Date:** 2026-06-24 · **Program:** [RetellAI Full API Parity Roadmap](./2026-06-24-retell-full-parity-program-roadmap.md) (Phase 1 of 7) · **Status:** Awaiting review

## 0. Summary

Phase 1 turns the existing-but-inert RetellAI-compat calling surface into a **frozen, conformance-verified, production-active** contract. Scope = the compat sub-app `apps/api/src/usan_api/compat` (mounted at root in `main.py:285`, matched after native `/v1`). Oracle = a **vendored** `openapi-final.yaml` (info.version **3.0.0**, **84** operations) + `retell-sdk==5.53.0`. All `file:line` references verified against `main` @ `fbb9a6b`.

**Goal:** a RetellAI client using the official Python/TS SDK or raw HTTP can place, retrieve, update, list, and stop phone calls, manage agents / Retell-LLMs / batch calls, read voices + concurrency, and receive signed webhooks — against our service, with zero code changes — and the contract is frozen so it cannot silently drift.

### Locked decisions (from brainstorming)
- **Delivery = 3 stacked sub-PRs** (each its own squash-merged PR): **1a** harness + freeze, **1b** new endpoints + plumbing, **1c** admin-UI + activation.
- **Production posture = activate now.** Issue a compat key and enable webhook delivery in prod. **Consequence:** the SSRF check-then-connect TOCTOU residual (`ssrf_guard.py:22-38`) **must be closed in Phase 1** (1c) — it is only safe to defer while delivery is off.
- Foundational principles (oracle, capability-bounded parity, documented-501, Bearer+RLS, webhook signature, error envelope, rate limiting, idempotency) are inherited verbatim from the roadmap §2 and are not restated here.

### Inherited constraints / gotchas
- **Single-org runtime.** Webhook delivery and live data-plane var injection assume one runtime org (the call plane is still single-org per the tenancy roadmap). Phase 1 does not change that.
- **Deploy mechanics.** New `COMPAT_*` env keys require: (a) the compose `api` `environment:` map AND the VM `.env` BOTH carry them (compose-passthrough), and (b) a VM `.env` refresh from Secret Manager `usan-prod-env` BEFORE the `v*` tag deploy (deploy never re-fetches secrets). Merged ≠ deployed.
- **admin-ui CI** runs `npm run typecheck` (tsc) + `npm run build`; local eslint/vitest don't typecheck — run `npm run typecheck` before pushing 1c.

---

## 1. Sub-PR 1a — Conformance harness + PENDING-FREEZE resolution + freeze existing endpoints

The foundation. Nothing else can claim "frozen" without the harness, so it lands first.

### 1a.1 Conformance harness
- **Location (net-new):** `apps/api/tests/compat/oracle/` (today `apps/api/tests/` is flat with ~14 `test_compat_*.py`; no `tests/compat/` package exists).
- **Vendor the oracle:** copy the pinned `openapi-final.yaml` (v3.0.0, 84 ops) to `apps/api/tests/compat/oracle/openapi-final.yaml` + a `SHA256SUMS` + `VERSION` (`3.0.0`). A test asserts the checksum so an unreviewed oracle swap fails CI. **Oracle-bump policy:** re-vendoring is a deliberate, human-reviewed PR that re-runs the full frozen suite.
- **Pin the SDK (gap — confirmed NOT installed):** add `retell-sdk==5.53.0` to a `[dependency-groups]` test/dev group in `apps/api/pyproject.toml` (+ `uv.lock`). Used to round-trip our JSON responses through `retell.types.*` Pydantic models.
- **What it validates:**
  1. **Path/verb coverage** — parse the vendored YAML; every one of the 84 ops must be either (a) served in-scope, (b) 501-stubbed in `unsupported.py`, or (c) an explicit allow-listed gap. This test is the **generator/guard for `unsupported.py`** and catches the six bare-404 Pri-1 endpoints (§1b).
  2. **Schema conformance** — for in-scope responses, build representative `Call`/`Agent`/`Voice`/`Concurrency` rows, serialize, and validate the dict against both the oracle component schema (jsonschema/`openapi-core`) and the `retell-sdk` model `.model_validate()`.
  3. **Request acceptance** — feed oracle-valid example bodies (incl. `override_agent_version='latest'`, `agent_override`, `call_time_window`) and assert 2xx (not 422).
- **FROZEN gate:** a `@pytest.mark.frozen` suite (runs in the existing "Lint Python"/pytest CI job). Any compat request/response schema diverging from the vendored oracle fails it. Plus a "surface-sum" test: in-scope routers + `unsupported.py` must equal exactly the 84-op oracle surface.

### 1a.2 PENDING-FREEZE resolutions (19 actionable; 2 are docstrings that simply become test-frozen)

| Marker (`file:line`) | Resolved frozen shape | Oracle |
|---|---|---|
| `schemas/calls.py:24` `override_agent_version` | **Widen** `int \| None` → `int \| str \| None` (numeric version OR tag like `latest`/`prod`). Highest-impact fix — int-only 422s `latest`. Numeric→that version; string→tag (MVP serves current). | `AgentVersionReference` |
| `call_create.py:50` idempotency dedupe | FREEZE as-is: `sha256(org,to,from,agent,packed)`, 201 on fresh+replay (safety superset; Retell has no idem key). | createPhoneCall |
| `call_create.py:78` contact metadata keys | FREEZE `name`/`external_id` as engine-private `metadata` keys (oracle metadata is opaque → any key is contract-safe). | metadata |
| `status_map.py:14` | FREEZE: never emit `not_connected`; BUSY/NO_ANSWER → `ended` + `dial_busy`/`dial_no_answer`. | V3CallBase enum |
| `status_map.py:36` FAILED | FREEZE `dial_failed` (valid member) over `error_unknown`. | DisconnectionReason |
| `schemas/calls.py:83` `user_sentiment` | FREEZE `str \| None` (null now); pin non-null vocab to title-case `Negative/Positive/Neutral/Unknown`. | CallAnalysis |
| `schemas/calls.py:103` `collected_dynamic_variables` | FREEZE `dict[str,str] \| None = None`. | V3CallBase |
| `schemas/calls.py:116` `latency` | FREEZE `dict \| None = None`; pin target per-category shape `{e2e:{p50..},asr:{..}}`. | CallLatency |
| `schemas/calls.py:28` `custom_sip_headers` | FREEZE accept+echo, **stringify** values (`additionalProperties:string`). SIP-INVITE injection is post-freeze. | metadata |
| `schemas/calls.py:35` update-call | **Enum-validate** `data_storage_setting` ∈ `everything\|everything_except_pii\|basic_attributes_only` (no-op behavior); `custom_attributes` passthrough `dict\|None`. | CallBase |
| `schemas/batch.py:44` `call_time_window` | FREEZE as TYPED echo matching `CallTimeWindow{windows[],timezone,day[]}`; validate + map `windows[0]`→native window, `day[]`→days_of_week. | CallTimeWindow |
| `schemas/batch.py` `scheduled_timestamp` | CONFIRMED CORRECT — response in **seconds**, request `trigger_timestamp` in **ms**. No change. | as cited |
| `voice_map.py:9` voice_id alias | FREEZE `retell-<Name>` prefix + 422-on-unhosted; add historical aliases lazily. | VoiceResponse |
| `schemas/voices.py:16` | **FIX**: `provider` → enum member `cartesia`; `gender` non-null `male`/`female` (oracle-required). accent/age/preview stay null. | VoiceResponse |
| `routers/agents.py:62` get-agent `?version` | FREEZE accept-and-serve-current (`current` == `latest_published`). | AgentVersionReference |
| `agent_bridge.py:274` publish version | FREEZE: accept `body.version` (advisory) but return server-authoritative auto-assigned number in `AgentResponse.version`. | agent_version |
| `routers/retell_llm.py:11` list prefix | FREEZE unversioned root + bare-array (matches list-agents precedent). | root-mounted mgmt |
| `routers/calls.py:131` filter breadth | FREEZE open `dict` request (don't narrow); implement `agent_id`; ignore unknown filters silently (contract-safe). Document real shape `agent=[AgentFilter]`. | CallFilter |
| `schemas/calls.py:66` transcript_object words | FREEZE `words=[]`; **pin `role`** enum `agent\|user\|transfer_target`. | Utterance |

### 1a.3 Existing-endpoint shape fixes (before freeze)

| Endpoint | Change |
|---|---|
| `POST /v2/create-phone-call` (`calls.py:59`) | Widen `override_agent_version` (above); accept `agent_override` + `ignore_e164_validation` (latter may no-op); stringify-echo `custom_sip_headers`. |
| `GET /v2/get-call/{id}` (`calls.py:77`) | **OMIT** `transcript_with_tool_calls` — our field is `str\|None`, oracle is `array<UtteranceOrToolCall>` (**genuine mismatch**). Don't freeze a wrong-typed string under an array name. `transcript` (string) + `transcript_object` (array) already correct. The array weaving is deferred to a later phase (documented parity gap). |
| `PATCH/POST /v2/update-call/{id}` (`calls.py:107`) | Add oracle field `override_dynamic_variables` (alias of `retell_llm_dynamic_variables` on this op); enum-validate `data_storage_setting`; keep `custom_attributes` passthrough. |
| `GET /get-agent-versions/{id}` (`agents.py:138`) | **WRONG SHAPE** — returns 4-field dicts. Re-serialize each version through `serialize_agent` → `array<AgentResponse>` (oracle `getAgentVersions` 200). |
| `POST /create-batch-call` (`batches.py:40`) | Type `call_time_window` against `CallTimeWindow`; validate + map. Keep `scheduled_timestamp` in seconds. |
| `GET /list-voices` + `GET /get-voice/{id}` (`catalog.py`) | Pin `provider`→`cartesia` enum; `gender` non-null `male`/`female`. |
| Others (get-call null fields, list-calls, retell-llm list, concurrency) | No blocking change; freeze as-is. |

**1a exit:** vendored oracle + checksum test; `retell-sdk` pinned; conformance suite green on all in-scope endpoints; all 19 markers resolved + the 2 docstrings test-frozen; FROZEN gate live in CI.

---

## 2. Sub-PR 1b — Pri-1 missing endpoints + 501-router regeneration + runtime plumbing

### 2b.1 The six Pri-1 endpoints (verified: no route, no 501-stub today → bare compat 404)

| Endpoint | Implementation |
|---|---|
| `POST /v2/register-phone-call` → 201 `CompatCall` | New route + `RegisterPhoneCallRequest`. Registers WITHOUT dialing: create a `Call` row with `call_status=registered`; **must NOT** call `outbound_calls.create_and_dispatch`. Add a test asserting the poller never auto-dials a `registered` row. Map `direction`, `agent_id`→`profile_override`. |
| `DELETE /v2/delete-call/{id}` → 204 | **Soft-archive + PHI-redact** (null transcript/recording, retain id/cost/timestamps/audit) — matches agent `delete==archive` and the HIPAA posture. RLS-scoped, PHI-free audit, 404 via `_load_call`. |
| `PATCH /v2/update-live-call/{id}` → `{success: bool}` | New route + `UpdateLiveCallRequest{fields_to_override, call_control}`. MVP: ack `success:true`, push `fields_to_override` to the live room (var injection, §2b.3). `call_control` verbs accepted but **no-op + documented-partial** this phase (frozen as accepted, behavior deferred). |
| `DELETE /delete-agent-version/{agent_id}` → 204 | Soft-remove an `AgentProfileVersion`; **guard the currently-published version** (refuse). 404 unknown agent/version. Verify the version selector (path vs query vs body) against the oracle param list during impl. |
| `POST /v2/list-agents` → `array<AgentResponse>` | DISTINCT from bare `GET /list-agents`. New route + `ListAgentsRequest{filter_criteria}`; reuse `agent_bridge.list_agent_profiles` + `serialize_agent`. |
| `POST /publish-agent/{agent_id}` → `AgentResponse` | Thin/body-less; delegate to `agent_bridge.publish_agent_version`; reconcile with `publish-agent-version` so both produce identical server-authoritative versioning. |

### 2b.2 501-router regeneration
Regenerate `unsupported.py`'s `_UNSUPPORTED` set **from the vendored oracle**: every one of the 84 ops not in the in-scope set must 501 at its **exact versioned path**. Fixes the verified drift (`/list-chat`→`/v3/list-chats`, `/add-voice`→`/add-community-voice`, `/search-voice`→`/search-community-voice`, `/create-test-case`→`/create-test-case-definition`, missing `/v2` on `/list-conversation-flows` & `/list-phone-numbers`, `/get-mcp-tools`→`/get-mcp-tools/{agent_id}`) and removes phantom stubs `/get-export-request` + `/create-test-run`. The 1a path-coverage test enforces this going forward.

### 2b.3 Runtime plumbing
**Boundary (verified):** `apps/api` and `services/agent` share no Python imports; api→agent is one-shot LiveKit dispatch metadata. Both new capabilities add a NEW api→agent channel = the **LiveKit room data plane**, targeted by `Call.livekit_room` (`db/models.py:145`).

- **(a) Force-hangup** (stop-call + abrupt update-live-call): new `livekit_dispatch.force_hangup(room)` → `lkapi.room.delete_room(...)` (disconnects all participants incl. the SIP/PSTN leg; best-effort, swallow room-not-found like `_delete_room`). Called from `compat.stop_call` when `not status_map.is_terminal(call.status)` and a room exists. `livekit-api>=1.0.0` already pinned in the api venv. **No agent change required.** Optional graceful seam: an RPC/data handler in `worker.entrypoint` running `check_in._hang_up` (say-goodbye → delete_room) on a `stop` signal.
- **(b) Mid-call var injection** (update-live-call): api side — keep the DB write AND `lkapi.room.send_data(SendDataRequest(room, data=json.dumps(new_vars), topic='dynamic_vars'))` (broadcast avoids needing the agent's participant identity). Agent side — in `worker.entrypoint` after `ctx.connect()` (`worker.py:327`, before `session.start` @ `:369`), register `room.on('data_received')` topic-filtered → parse vars → merge → re-run `prompt_vars.substitute` → `Agent.update_instructions(...)`. **Shared wire contract:** a topic name + JSON shape + RPC method, duplicated on both sides (precedent: `prompt_vars.BUILTIN_DEFAULTS` mirror).

**1b exit:** all six endpoints implemented + conformance-green + frozen; 501-router == oracle-minus-in-scope (surface-sum test green); force-hangup + var-injection unit-tested with mocked `RoomService`/room; the agent data-plane receiver unit-tested (re-substitute → `update_instructions`).

---

## 3. Sub-PR 1c — Admin-UI compat-key management + SSRF hardening + prod activation

### 3c.1 Admin-UI compat-key management (gap — no screen today)
A **super-admin-only** screen (gated like the Org console, NOT org-admin), against the already-shipped `routers/admin_compat_keys.py` (`/v1/admin/compat-keys`, `Depends(require_super_admin)`, table `compat_api_keys` live since migration 0036 — no backend/migration work):

| Flow | Backend | UI |
|---|---|---|
| Issue (once-shown) | `POST /v1/admin/compat-keys {label?}` → 201 `{…,token}` | Render `token` with copy + "shown once, store now" (mirror invite copyable-link UX #103). Issues for the super-admin's active org. |
| List | `GET /v1/admin/compat-keys` → `list[CompatKeyResponse]` (token omitted) | Table: `key_prefix`, `status`, `label`, `created_at`, `last_used_at`, `revoked_at`. |
| Revoke | `DELETE /v1/admin/compat-keys/{id}` → 204 | Revoke-confirm dialog (mirror invite revoke #103). |

### 3c.2 SSRF TOCTOU hardening (pulled in by activate-in-prod)
Close the check-then-connect residual in `ssrf_guard.py:22-38`: a custom httpx transport that **resolves the host once, validates the resolved IP against the allow-list + private/link-local/loopback denylist, then pins the connection to that vetted IP** (no re-resolution at connect). Keep `follow_redirects=False`, ports 443/8443 only. This is the gate that makes enabling delivery against external receivers safe.

### 3c.3 Settings templating (gap — verified ZERO `COMPAT_` refs in `infra/`)
All five settings exist in `settings.py:296-308` but are absent from every compose env map + `.env*` template, so on the VM they can only take code defaults (no operator lever). Template all five — `COMPAT_DOCS_ENABLED`, `COMPAT_WEBHOOK_ALLOWED_HOSTS`, `COMPAT_DEFAULT_TIMEZONE`, `COMPAT_KEY_RATE_LIMIT`, `COMPAT_WEBHOOK_DELIVERY_ENABLED` — into the compose `api` `environment:` map + `.env` templates, mirroring the `DOCS_ENABLED`/`SCHEDULER_POLLER_ENABLED` pattern (dev-ON, prod-overlay pinning).

### 3c.4 Activation (prod)
1. **Deploy** the Phase-1 `v*` tag — but FIRST refresh the VM `/opt/usan/infra/.env` (+ Secret Manager `usan-prod-env`) with the five `COMPAT_*` keys (deploy doesn't re-fetch secrets).
2. **Issue a compat key** via the new UI (super-admin). Token shown once. Auth + RLS + the `600/min` `compat` rate-limit bucket (`main.py:195`) are already wired.
3. **Enable webhooks:** set `COMPAT_WEBHOOK_ALLOWED_HOSTS` (empty → no webhook ever fires) and flip `COMPAT_WEBHOOK_DELIVERY_ENABLED=true`. Signature is byte-faithful `x-retell-signature` (`v=<ts_ms>,d=<hmac>`), verifiable by an unmodified customer `Retell.verify()`.
4. **Smoke** with the real `retell-sdk` against prod (create→get→update→list→stop + reads + a verified webhook).

**1c exit:** super-admin can issue/list/revoke keys in the UI; SSRF guard pins resolved IP; all five settings templated + on the VM; prod live with an issued key + webhook delivery on; real-SDK e2e smoke green against prod.

---

## 4. Resolved open questions

1. **delete-call** → soft-archive + PHI-redact (not hard-delete). 2. **delete-agent-version selector** → verify oracle param source during impl; guard published version. 3. **publish-agent vs -agent-version** → both → `agent_bridge.publish_agent_version`, identical server-authoritative versioning. 4. **update-live-call `call_control`** → accept + no-op + documented-partial this phase. 5. **transcript_with_tool_calls** → OMIT (documented gap), array weaving later phase. 6. **register-phone-call** → `registered` status; test asserts no auto-dial. 7. **SSRF TOCTOU** → IN SCOPE (1c), required by activate-in-prod. 8. **batch call_time_window** → typed echo + map `windows[0]`/`day[]`; document any native-window expressiveness gap. 9. **single-org runtime** → confirmed Phase-1 constraint, unchanged. 10. **oracle vendoring** → checksum-pinned + human-reviewed bump policy.

---

## 5. Test plan
- **Unit** — every PENDING-FREEZE resolution (override union accepts int/`latest`; title-case sentiment; voice provider/gender enums; data_storage enum reject; `get-agent-versions` full `AgentResponse[]`; `transcript_with_tool_calls` omitted) + the six new routes (register no-dial, delete-call archive, update-live-call `{success}`, delete-agent-version guard, POST list-agents filter, publish-agent) + force-hangup helper + agent data-plane receiver (re-substitute → `update_instructions`) with mocked `RoomService`/room.
- **Conformance** (§1a.1) — 84-op path/verb coverage + `retell-sdk==5.53.0` model round-trip on every in-scope response + oracle-valid request acceptance. `@pytest.mark.frozen` in CI.
- **Real-SDK e2e smoke** — issue key, point real `retell-sdk` at the api, run create→get→update→list→stop + agent/llm/voice/concurrency reads + `Retell.verify()` on an emitted webhook. (`-n0` serial when debugging per CLAUDE.md.)

## 6. Out of scope (later phases)
Web calls (P3), chat/SMS + chat-agents (P4), knowledge base (P5), conversation flow (P6), test-suites/MCP/voice-clone/playground/phone-numbers/exports (P2/P7) — all remain documented-501 at correct paths. `transcript_with_tool_calls` array, `custom_sip_headers` SIP-INVITE injection, and `update-live-call` `call_control` verbs are explicitly deferred.

## 7. Risks
- **Activate-in-prod widens PHI-egress + attack surface with no client yet** (accepted decision) — mitigated by SSRF IP-pinning + allow-list + rate-limit + PHI-free audit + once-shown keys.
- **api↔agent wire contract** (var injection) is duplicated by hand across the boundary — a drift risk; mitigate with a shared-shape unit test on both sides.
- **Oracle drift** (Stainless regenerates near-daily) — mitigated by the checksum-pinned vendored YAML + reviewed-bump policy.
- **Batch window expressiveness** — native window may not fully express oracle `CallTimeWindow`; freeze typed-echo + partial map + document the gap rather than silently dropping fields.
