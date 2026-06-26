# RetellAI Parity Phase 2 — Phone Numbers + Exports — Design

**Date:** 2026-06-25 · **Type:** Phase design spec (one `spec → plan → implementation` cycle, one squash-merged PR) · **Status:** Approved + adversarial-critique-revised, awaiting spec review

> Phase 2 of the RetellAI full-parity program (roadmap: `docs/superpowers/specs/2026-06-24-retell-full-parity-program-roadmap.md`). Builds on the compat surface activated/frozen in Phases 1a (049cc89) / 1b (71b401e) / 1c (33f1ed1). Grounded in the vendored oracle `apps/api/tests/compat/oracle/openapi-final.yaml` (v3.0.0) + `retell-sdk==5.53.0`, and revised against a 5-lens adversarial critique (oracle-conformance, internal-consistency, pattern-fidelity, completeness, security/PHI) plus a code-grounded verification of the compat surface's not-found/validation/501 conventions.

## 1. Goal

Close the RetellAI **phone-number** surface (6 ops) and the **export** surface (1 op) on the compat sub-app so a RetellAI client repointing its base URL operates with zero code changes across these endpoints — serving everything we can back honestly, and returning a correct-path documented-501 only where the engine genuinely cannot perform the action (DID purchase).

**One-sentence scope:** real per-org CRUD for `import`/`get`/`update`/`list`/`delete` phone numbers over a new `TenantScoped`+RLS table (bindings persisted, **not yet honored** at call-routing time — documented, not faked), `create-phone-number` as a documented-501, and `GET /v2/list-export-requests` as a shape-conformant empty-list stub.

## 2. Posture decisions (locked in brainstorming)

| Fork | Decision | Rationale |
|---|---|---|
| **Phone-numbers posture** | **Real CRUD, bindings deferred.** Serve import/get/update/list/delete for real; `create-phone-number` → documented-501; agent bindings persisted + echoed but documented as **not yet honored** at call time. | Highest parity achievable with **no new external spend, no Telnyx Numbers-API client, no live-dial rewire**. Honoring is partly blocked by the single-org runtime call-plane anyway (no client onboarded). Avoids both the "ship-nothing" 501 and the silent "stored-but-cosmetic" trap. |
| **Export scope** | **Empty-list stub.** `GET /v2/list-export-requests` → `{items: [], has_more: false}`. | The oracle has **no create/get-by-id** export op, so a parity client cannot enqueue an export through the API — a fresh org genuinely has none. The only call a client can make (list) succeeds with a valid shape. |
| **Binding honoring** | **Deferred (documented).** | Outbound dial uses a single global `settings.telnyx_caller_id` and a process-wide org-blind trunk cache; inbound routing is runtime-only LiveKit SIP state. Honoring touches the live-call path for zero current consumers and fights the single-org runtime. |
| **Not-found + envelope** | **Match the house convention** (404 not-found, int-status envelope), not the oracle's 422/`"error"`. | The entire frozen 1a–1c surface uses 404 + int status. A one-resource deviation would split-brain the surface. Both house-vs-oracle deviations are recorded in §13 for a dedicated surface-wide conformance pass. |
| **Deploy** | **Merge-to-main only (no `v*` tag).** | Migration 0040 is inert until an operator cuts a tag. No new env keys. Matches the 1c posture. |

## 3. Non-Goals (explicit, documented follow-ups)

- **`create-phone-number`** — needs a net-new Telnyx Numbers-API client + a number-management key + real DID purchase + programmatic SIP routing. Stays a correct-path 501.
- **Telnyx Numbers-API client** (`telnyx_numbers.py`: search/order/release) — not built this phase.
- **Binding honoring at call time** — outbound dial-path rewire (`livekit_dispatch.py`) + inbound DID→agent map (`provision-sip-inbound.sh` path). Named follow-up gated on the multi-org call-plane.
- **Real async export job** — `export_requests` table + poller + GCS CSV upload + USAN-only enqueue endpoint. Promote from the empty-list stub only when a concrete export consumer exists.
- **Admin-UI for numbers** — no admin screen this phase (the compat API is the parity surface).
- **Surface-wide not-found/envelope conformance fix** (see §13) — out of scope; recorded for a dedicated pass.

## 4. Architecture

A new compat resource module pair plus one new owner-DDL migration and one new domain table — nothing outside `apps/api`. No changes to `services/agent`. No new settings, no poller, no GCS write.

```
apps/api/
  src/usan_api/
    db/models.py                       # + PhoneNumber(Base, TenantScoped)
    repositories/phone_numbers.py      # NEW: RLS-scoped CRUD repo
    logging_config.py                  # - mask the E.164 path segment in compat access logs (§9)
    compat/
      schemas/phone_numbers.py         # NEW: request/response Pydantic models + AgentWeight
      schemas/export_requests.py       # NEW: empty-list response model
      routers/phone_numbers.py         # NEW: import/get/update/list/delete handlers (own module-local _audit)
      routers/export_requests.py       # NEW: list-export-requests (empty) handler (own _audit)
      routers/unsupported.py           # - remove the 5 served phone paths + export path from _UNSUPPORTED; KEEP create-phone-number
      app.py                           # + app.include_router(...) for both new routers in build_compat_app
  migrations/versions/0040_*.py        # NEW: phone_numbers table + FORCE RLS + GRANT TO usan_app (owner-DDL; alembic.ini at apps/api/)
  tests/compat/
    test_phone_numbers_frozen.py       # NEW: per-endpoint conformance + trap + RLS tests
    test_export_requests_frozen.py     # NEW: empty-list conformance
    test_surface_coverage.py           # - update served/501 sets; KNOWN_GAPS stays frozenset()
```

Notes verified against the real tree: migrations live in `apps/api/migrations/versions/` (latest `0039_call_archived_at.py`), **not** an `alembic/` package; `_audit` is a **per-router module-local** function (no shared helper) — each new router defines its own PHI-free `_audit`; new routers are wired via `app.include_router(...)` in `build_compat_app` (`compat/app.py`).

## 5. Global Constraints (every task inherits these)

- **httpx 0.28.1** (pinned); no new external HTTP calls in this phase.
- **Oracle omits null optionals** → `exclude_none` is load-bearing on every serialization. Use **Convention A** (`response_model=PhoneNumberResponse` + `response_model_exclude_none=True`) for the single-object responses.
- **Error envelope** `{"status": <int HTTP code>, "message": str}` via `CompatError` only (never `HTTPException`) — matching the existing house envelope (`compat/errors.py`). Request-body validation failures map to **422** via the global `RequestValidationError` handler (`compat/errors.py`), first-offending-field only, no PHI.
- **Not-found / cross-org → 404** with a short `"<resource> not found"` message, matching the established convention (`calls.py`/`agents.py`/`catalog.py` all raise `CompatError(404, ...)`). This deviates from the oracle's 422 — see §13.
- **No faked behavior.** Persisted-not-honored bindings are echoed truthfully and documented as not-yet-routed; no endpoint returns 200 while pretending to route a call.
- **No PHI / no secrets in logs.** `sip_auth_password` is write-only: never echoed in any response (incl. `sip_outbound_trunk_config`), never logged, never in audit detail. The raw E.164 appears in the request **path** (`/{phone_number}`) — mask it in the compat access log (§9). Each router's `_audit` binds org id + op + an internal/opaque number reference only.
- **RLS for free.** Org from `request.state.compat_org_id` (bearer compat-key), never the body; `Depends(get_compat_db)`. The new table is `TenantScoped` + FORCE RLS.
- **AgentWeight validation is structural + resolution only — no sum-to-1.** The oracle `AgentWeight` schema bounds each `weight` (`0 < weight ≤ 1`) but declares **no sum constraint**; enforcing sum==1 would 422 valid client traffic, so it is **not** enforced. See §6.
- **Stored webhook/SIP URLs are SSRF-validated at write** (`ssrf_guard.validate_webhook_url`) → 422 on invalid, mirroring the existing webhook-endpoint registration path. See §6/§7.
- **`KNOWN_GAPS` stays `frozenset()`.** Every oracle path remains served-or-501 at its **exact verbatim path + param name** (`{phone_number}`, `/v2/` on list only).
- **Owner-DDL migration** (0040) models itself on **0037** (the new-`TenantScoped`+FORCE-RLS-table precedent) — **including `GRANT SELECT, INSERT, UPDATE, DELETE ON phone_numbers TO usan_app`** — not on 0031/0034 (which add RLS to pre-existing, already-granted tables). Runs as the `usan` owner on deploy (handled since #124).
- **Merge-to-main only**, no `v*` tag; inert until an operator deploys.
- **Conventional commits**, scope `api`. Attribution disabled (no `Co-Authored-By`, no 🤖 footer).

## 6. Data model — migration 0040 + `PhoneNumber` ORM

New table `phone_numbers`, `TenantScoped` (auto `organization_id` default from `app.current_org`) + FORCE RLS, per-org-unique on `phone_e164`, **with a `GRANT … TO usan_app`** (modeled on 0037, the new-TenantScoped-table precedent — without the grant every CRUD op hits "permission denied" under the least-priv `usan_app` runtime role).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | internal only; **never surfaced** (the E.164 is the API identifier) |
| `organization_id` | uuid | TenantScoped default `default_org_id()`; RLS predicate; CASCADE on org delete |
| `phone_e164` | text | the identifier, stored **verbatim** from the request; `uq_phone_numbers_e164_org (organization_id, phone_e164)` |
| `phone_number_type` | text | `retell-twilio` \| `retell-telnyx` \| `custom`; imported numbers → `custom` |
| `phone_number_pretty` | text null | **server-derived** from `phone_e164` (national display format); never a client input; omitted-when-null |
| `nickname` | text null | |
| `area_code` | int null | **reserved for future `create`**; always null in Phase 2 (import/update never set it), so always omitted via exclude_none. Retained because the oracle response component declares it |
| `inbound_webhook_url` | text null | SSRF-validated at write (§7) |
| `inbound_sms_webhook_url` | text null | update/response only; SSRF-validated at write |
| `allowed_inbound_country_list` | text[] null | |
| `allowed_outbound_country_list` | text[] null | |
| `fallback_number` | text null | |
| `transport` | text null | one of `TLS`/`TCP`/`UDP` (default `TCP` on echo); validated at write |
| `termination_uri` | text null | SIP outbound trunk target (from import/update) |
| `sip_auth_username` | text null | echoed inside `sip_outbound_trunk_config` |
| `sip_auth_password` | text null | **write-only**: never echoed/logged/audited; stored for future honoring. Hardening note: prefer app-side encryption (Fernet/KMS) if a secret-at-rest mechanism exists; else RLS-isolated plaintext with a documented follow-up |
| `inbound_agents` | jsonb null | list of AgentWeight |
| `outbound_agents` | jsonb null | list of AgentWeight |
| `inbound_sms_agents` | jsonb null | list of AgentWeight (update/response only) |
| `outbound_sms_agents` | jsonb null | list of AgentWeight (update/response only) |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | → `last_modification_timestamp` (epoch ms) on serialize |

**AgentWeight** (jsonb element + Pydantic sub-model): `{agent_id: str (minLen 1, required), weight: number (0 < weight ≤ 1, required), agent_version?: int(≥0) | str-tag}`. Validation on write:
- **Structural** (Pydantic) → 422 on malformed: `agent_id` non-empty, `weight` in `(0, 1]`, `agent_version` int-or-tag.
- **Resolution** → 422 (treated as a body-validation failure, **not** a 404 path-not-found): each `agent_id` must decode via `compat/ids.decode_agent_id` (which itself raises 422 on a malformed id) **and** resolve to a non-archived `AgentProfile` in the caller's org via the RLS-scoped `agent_profiles` repo (`get_profile` → `None`/archived ⇒ `CompatError(422, "invalid request: …")`). Do **not** route through `agent_bridge.get_agent_profile` (it raises 404).
- **No sum-to-1 rule.** `null` list = unset/no binding; `[]` = clear bindings; neither triggers any cross-element check.

**Agent-reference lifecycle:** bindings reference agents **loosely** (resolved at write, no FK). If an agent is later deleted/archived, stale bindings persist until the number row is updated/deleted; cleanup is deferred to the honoring follow-up. `DELETE /delete-phone-number` is a **hard delete** (removes the row and any stored `sip_auth_password`), not a soft-archive.

## 7. Endpoints — exact oracle contract

All single-object responses serialize `PhoneNumberResponse` via Convention A. Org-scoped through `get_compat_db`. Path/param strings are oracle-verbatim. Each router defines its own PHI-free `_audit`.

**`PhoneNumberResponse`** (shared, oracle component — exists at `#/components/schemas/PhoneNumberResponse`) — required: `phone_number` (str), `phone_number_type` (enum), `last_modification_timestamp` (int epoch ms). Optional (omit-when-null): `phone_number_pretty` (str, **not** nullable), `area_code` (int, **not** nullable — always omitted in Phase 2), `nickname` (str,null), `inbound_webhook_url` (str,null), `inbound_sms_webhook_url` (str,null), `allowed_inbound_country_list` / `allowed_outbound_country_list` (array,null), `inbound_agents`/`outbound_agents`/`inbound_sms_agents`/`outbound_sms_agents` (array<AgentWeight>,null), `sip_outbound_trunk_config` (obj,null — `{termination_uri, auth_username, transport}`, all str/null, **no `auth_password` key ever**), `fallback_number` (str,null). SDK round-trip: `retell.types:PhoneNumberResponse`.

**`sip_outbound_trunk_config` emit rule:** emit the object when **any** of `termination_uri`/`sip_auth_username`/`transport` is non-null; each sub-field omitted-when-null; `transport` defaults to `TCP` when the trunk config is emitted but transport is unset. `transport` is surfaced **only nested** here, never as a top-level response field.

### 7.1 `POST /import-phone-number` → 201
Request required `[phone_number (E.164, minLen 1), termination_uri (str)]`. Optional: `ignore_e164_validation` (bool, **default true** when omitted — JSON bool literal only; string `"true"`/`"false"` → 422 via the body validator), `sip_trunk_auth_username`, `sip_trunk_auth_password` (**note `sip_trunk_` prefix** — diverges from update), `inbound_agents`/`outbound_agents` (array,null), `nickname` (str, **not** nullable), `inbound_webhook_url` (str,null), `allowed_inbound/outbound_country_list` (array,null), `transport` (str,null). **No** `area_code`/`number_provider`/`country_code`/`toll_free`/`fallback_number`/sms-agents.

Behavior: `phone_e164` stored **verbatim** (the request string is the identifier; with `ignore_e164_validation` default-true there is no normalization — distinct strings are distinct numbers, and get/update/delete `{phone_number}` must match byte-for-byte). When `ignore_e164_validation` is explicitly `false`, validate E.164 and return **400** on an invalid format (in-oracle vocab). Imported → `phone_number_type = "custom"`. `sip_outbound_trunk_config` in the response is built from `termination_uri`+`sip_trunk_auth_username`+`transport`. Duplicate `phone_e164` for the org (uniqueness violation) → **400** (in-oracle vocab; RetellAI's exact re-import semantics are unverified, 400 chosen as the conservative in-vocab error — see §13).

**Errors:** **400** (invalid E.164 when not ignored; duplicate), **401**, **422** (malformed body / AgentWeight structural+resolution failure — the surface-wide validation status, applied by the global handler), **500**. (The oracle import op lists 400/401/500; 422 is the surface-wide validation behavior already emitted by `compat/errors.py` for any malformed body.)

### 7.2 `GET /get-phone-number/{phone_number}` → 200
Path `phone_number` (E.164, verbatim match). No body/query. **404** on not-found / cross-org (RLS), per house convention (§5/§13). Errors **400/401/404/500** (404 is the house behavior; the oracle lists 422 — see §13).

### 7.3 `PATCH /update-phone-number/{phone_number}` → 200
Path `phone_number`. Body all-optional **superset**: `inbound_agents`, `outbound_agents`, `inbound_sms_agents`, `outbound_sms_agents` (array,null), `nickname` (str, **nullable here** — inverted vs import), `inbound_webhook_url` (str,null), `inbound_sms_webhook_url` (str,null), `allowed_inbound/outbound_country_list` (array,null), `termination_uri` (str, **not** null), `auth_username`, `auth_password` (**note: no `sip_trunk_` prefix** — diverges from import), `transport` (str,null), `fallback_number` (str,null — explicit null removes it). Merge semantics (only provided fields change). Webhook URLs SSRF-validated; AgentWeight validated per §6. Unknown/cross-org `{phone_number}` → **404** (house convention; **not** an upsert). Errors **400/401/404/422/500**.

### 7.4 `GET /v2/list-phone-numbers` → 200
Query: `limit` (int, default 50, max 1000), `sort_order` (enum ascending|descending, default descending), `pagination_key` (str **opaque encoded cursor**, reusing the established compat cursor convention). **No filter params.** `/v2/` prefix on **list only**. Response = paginated envelope `{items: [PhoneNumberResponse], pagination_key?, has_more?}` (omit-when-null), **not** a bare array. Keyset pagination over `(created_at, id)` behind the opaque cursor.

**Conformance:** the v2 paginated wrapper has **no named oracle component** (`PhoneNumberListResponse` exists only as an SDK type) — `assert_conforms` must be called **per item** with `"PhoneNumberResponse"`, and the envelope (`items`/`pagination_key`/`has_more`) asserted via explicit field checks, mirroring `test_list_agents_v2.py`. SDK round-trip of the whole wrapper may use `assert_sdk_roundtrip(payload, "retell.types:PhoneNumberListResponse")`. A malformed/foreign cursor is handled safely (empty page) — no off-oracle 400/422 (the oracle list vocab is only 401/500). Errors **401/500**.

### 7.5 `DELETE /delete-phone-number/{phone_number}` → 204
Path `phone_number`. Bare `Response(status_code=204)`, **empty body** (calls/agents delete convention). Hard-deletes the row (and stored secret). **404** on not-found / cross-org. Errors **401/404/500** (404 house behavior; oracle lists 422 — §13).

### 7.6 `POST /create-phone-number` → 501 (documented)
Stays in `_UNSUPPORTED` at its exact path. Uses the **existing** stub format — `CompatError(501, f"not_supported: {endpoint}")` → message `"not_supported: /create-phone-number"` (leading slash, space; do **not** invent a new string) — and the standard envelope `{"status": 501, "message": …}` (int status). A frozen test asserts it is still 501 with that exact message and documents the reason (Telnyx DID purchase unavailable). Counts as served-or-501 → `KNOWN_GAPS` stays empty.

### 7.7 `GET /v2/list-export-requests` → 200 (empty-list stub)
Query: `limit`/`sort_order`/`pagination_key` (accepted, validated, **ignored** — any cursor value yields the empty page). Returns `{items: [], has_more: false}` (omit `pagination_key` via exclude_none). **Conformance:** there is **no `ExportRequestListResponse` oracle component** (it exists only as an SDK type) — do **not** pass it to `assert_conforms`. Validate the wrapper keys directly; SDK round-trip may use `assert_sdk_roundtrip(payload, "retell.types:ExportRequestListResponse")`. The inline item schema is pinned in the test loader as the full oracle field set — `export_request_id` (str), `channel` (enum `call`|`chat`), `status` (enum `created`|`processing`|`completed`|`error`), `url` (str), `created_timestamp` (int epoch), `timezone` (str) — even though no rows are emitted, so the loader is a genuine contract lock. Errors **400/401/429/500**. Confirm the phantom `/get-export-request` stub is absent (no such oracle op).

## 8. Surface coverage

Move the 5 served phone paths (`import`, `/v2/list-phone-numbers`, `get/{phone_number}`, `update/{phone_number}`, `delete/{phone_number}`) + `GET /v2/list-export-requests` out of `_UNSUPPORTED` **and** add them as real routes in the same change; **keep** `POST /create-phone-number` in `_UNSUPPORTED`. `tests/compat/test_surface_coverage.py` (`test_501_stub_paths_match_oracle_exactly`) updated so every oracle path is served-or-501 with `KNOWN_GAPS == frozenset()`. Path strings + param names stay oracle-verbatim.

## 9. Honoring-deferred + PHI-in-path — explicit, not silent

- **Binding trap** (stored-but-not-routed returning 200) → mitigated by **documentation, not faking**: the compat OpenAPI route descriptions state that `inbound_agents`/`outbound_agents`/sms-agent bindings are **persisted and echoed but not yet honored at call-routing time** (runtime call-plane is single-org; outbound dial uses the global caller-id). A short note in `docs/deployment/` records the deferral and the named follow-up (outbound dial-path rewire + inbound DID→agent map). No endpoint fakes routing behavior; bindings round-trip truthfully.
- **Raw E.164 in the request path** — the oracle forces `{phone_number}` (literal E.164) as the path param, so it **cannot** be opaque-encoded like `call_id` without breaking parity. Mitigation: extend the access-log interceptor (`logging_config.py`, which today drops only `/health`) to **mask the E.164 segment** of `*-phone-number/*` request paths before the line reaches the sink (e.g. `/get-phone-number/+1*******234`), so the raw number is not persisted in Cloud Logging. The explicit `_audit` id-only design stands.

## 10. Testing

- **Frozen conformance per endpoint** (`pytest.mark.frozen`, `compat_client`/`compat_headers` fixtures): single-object payloads via `assert_conforms(payload, "PhoneNumberResponse")` + `assert_sdk_roundtrip(payload, "retell.types:PhoneNumberResponse")` for import/get/update; **list** via per-item `assert_conforms("PhoneNumberResponse")` + explicit envelope-key assertions (+ optional `assert_sdk_roundtrip("retell.types:PhoneNumberListResponse")`); **export-list** via wrapper-key assertions + the inline-item loader (+ optional `assert_sdk_roundtrip("retell.types:ExportRequestListResponse")`).
- **Field-name / nullability trap tests:** import accepts `sip_trunk_auth_*`, update accepts `auth_*`; the response (incl. `sip_outbound_trunk_config`) **never has an `auth_password` key**; `nickname` nullable on update but not import; sms-agent fields + `inbound_sms_webhook_url` present on update/response only; `area_code` null/omitted for imported numbers.
- **AgentWeight validation:** malformed (`weight` out of `(0,1]`, empty `agent_id`) → 422; unresolvable/archived `agent_id` → 422; **no** sum-to-1 rejection (a single agent with `weight 0.5` is accepted); `null` vs `[]` binding semantics.
- **E.164 handling:** `ignore_e164_validation` default-true stores verbatim; string `"true"`/`"false"` literal → 422; explicit `false` + invalid format → 400; a format-collision import (`+1415…` vs `1415…`) yields two distinct rows each addressable by its exact string.
- **Secret hygiene:** `sip_auth_password` absent from every response, from `_audit` detail, and from log records.
- **Binding round-trip:** import/update with agents, then `get` echoes the same AgentWeight lists (truthful persistence).
- **RLS isolation:** cross-org get/update/delete → 404; list shows only the caller-org's numbers; a `usan_app`-scoped session can CRUD the table (grant present).
- **Lifecycle:** import → get → update → list → delete round-trip; delete returns **204 with empty body**; duplicate import → 400.
- **Surface coverage:** served/501 sets correct, `KNOWN_GAPS` empty, `create-phone-number` still 501 with message `"not_supported: /create-phone-number"`.
- **Export empty-list:** shape-conformant, `items == []`, `has_more == false`, no `pagination_key` key, any `pagination_key`/`limit` input ignored.

## 11. Deploy & ops

- **Migration 0040** is owner-DDL (table + FORCE RLS + **`GRANT … TO usan_app`**, modeled on 0037) → runs as `usan` owner on deploy (handled since #124). `down_revision` chains off 0039. Inert until a `v*` tag.
- **No new env keys, no compose changes, no Secret Manager changes** — numbers CRUD is gated by the existing compat-key auth; no poller; the export stub touches no infra.
- Merge-to-main only this phase.

## 12. Risks (carried from grounding + critique)

- **Binding-functionality trap** — mitigated by §9 (documented, not faked).
- **Field-name / nullability traps** — pinned in §7 + §10 trap tests.
- **Owner-DDL migration + grant** — must model 0037 (new-table) incl. the `usan_app` grant, or every number op 500s under the least-priv runtime role (cf. memory `migrations_need_owner_not_usan_app`).
- **Surface-coverage invariant** — `KNOWN_GAPS` must stay empty; verbatim paths/params; served paths added as real routes in the same change that removes their stubs.
- **`sip_auth_password` at rest** — write-only, RLS-isolated, never echoed/logged/audited; encryption-at-rest flagged as a hardening follow-up; removed on hard-delete.
- **Stored webhook/SIP URL SSRF** — `inbound_webhook_url`/`inbound_sms_webhook_url`/`termination_uri` validated via `ssrf_guard.validate_webhook_url` at write (422), so a future honoring phase cannot egress to internal hosts/the metadata endpoint. (Allow-list enforcement is deferred to delivery/honoring time.)
- **Raw E.164 in path/logs** — masked in the access log (§9); cannot be opaque-encoded without breaking parity.
- **Off-oracle validation status (422)** — the surface-wide `RequestValidationError`→422 behavior applies to import despite the oracle's per-op 400/401/500 list; this matches every other compat op and is not Phase-2-specific.

## 13. Known surface-wide deviations (recorded, not fixed here)

These pre-date Phase 2, span the whole compat surface, and are deliberately **not** changed here (a one-resource fix would split-brain the surface). Recorded for a dedicated conformance pass:

1. **Not-found status: house `404` vs oracle `422`.** The frozen 1a–1c surface raises `CompatError(404, "<resource> not found")` for unknown/cross-org resources, whereas the oracle declares `422` ("Cannot find requested asset under given api key") for get/update/delete ops. Phase 2 matches the house `404` for consistency.
2. **Error-envelope `status`: house int vs oracle string `"error"`.** The house envelope is `{"status": <int HTTP code>, "message": str}`; the oracle's error schema declares `status` as the string enum `["error"]`. Phase 2 keeps the int-status house envelope.

Both should be resolved surface-wide (with a re-freeze) in a dedicated conformance task, not piecemeal.
