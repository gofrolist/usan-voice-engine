# RetellAI Parity Phase 3 — Web Calls (LiveKit WebRTC) — Design

**Date:** 2026-06-26
**Program:** RetellAI full-API-parity (Phase 3 of 7). See roadmap
`docs/superpowers/specs/2026-06-24-retell-full-parity-program-roadmap.md`.
**Predecessors:** Phases 1a/1b/1c (core calling + activation) and 2 (phone numbers +
exports), all merged, none deployed (gated on a `v*` tag).
**Builds on:** the compat sub-app, the conformance oracle, and the LiveKit dispatch
helpers already present in `apps/api`.

> This revision incorporates an adversarial 5-lens critique that line-verified every
> reuse claim against the real code. Notable grounded corrections vs the first draft:
> the existing `dispatch_agent` is **SIP-gated** and unusable for web (→ a new
> `dispatch_web_agent`); web `resolved_vars` are **empty** (no contact); the
> agent-resolution gate is the existing `register`-path pattern (reused inline, not
> extracted); the un-honored fields persist under a **second reserved var key** so the
> echoed `metadata` stays pristine.

---

## 1. Goal

Serve `POST /v2/create-web-call` so a RetellAI client can create a browser-joinable
voice call against our stack: the response carries a real, working LiveKit WebRTC
`access_token` the client's frontend uses to join a room where our agent answers.
This is a **live, end-to-end** capability, not a stub.

The single oracle operation in scope is `createWebCall` (`POST /v2/create-web-call` →
`201 V2WebCallResponse`). `get-call` / `list-calls` already exist (Phase 1a/1b) and
must remain conformant once they begin returning web-typed calls — addressed by the
serializer rule in §5, not by new endpoints.

---

## 2. Posture decisions (locked with the user)

1. **Execution posture = live end-to-end.** `apps/api` mints a real browser
   `access_token`, creates the LiveKit room, and dispatches the agent; the
   `services/agent` worker gains a web branch so a browser using the token has a
   real conversation. Validated at deploy, like every prior phase (merged ≠ live).

2. **Heavier optional request fields = accept + persist, not honored.**
   `agent_override`, `current_node_id`, and `current_state` are accepted (so SDK
   clients repoint with zero changes) and **persisted server-side for audit** (under a
   reserved, never-echoed var key — §7 step 2), but not acted on. Honoring them is
   Phase 6 territory. There is no conformant field on the RetellAI call object to echo
   them in, so "persist" means retained-for-audit, **not** reflected in the response.
   Recorded as a deviation in §11.

---

## 3. Oracle contract (verified against the vendored `openapi-final.yaml` v3.0.0)

### 3.1 Request — `CreateWebCallRequest`

| field | type | req | notes |
|---|---|---|---|
| `agent_id` | string, `minLength: 1` | **yes** | the only required field |
| `agent_version` | `AgentVersionReference` (int \| string tag) | no | resolves to the latest published version when omitted (oracle `AgentVersionReference`) |
| `agent_override` | `AgentOverrideRequest` (object) | no | **accept + persist (audit), not honored** |
| `metadata` | object (free-form) | no | storage-only, echoed back on the call object |
| `retell_llm_dynamic_variables` | object<string,string> | no | injected into the Response Engine prompt |
| `current_node_id` | string, nullable | no | **accept + persist (audit), not honored** |
| `current_state` | string, nullable | no | **accept + persist (audit), not honored** |

### 3.2 Response — `V2WebCallResponse` (HTTP **201**)

`allOf [ { required: [call_type, access_token], properties: { call_type, access_token } }, V2CallBase ]`
(oracle lines 9136–9157).

- `call_type`: string enum, single value `'web_call'` (required).
- `access_token`: string (required) — "Access token to enter the web call room. This
  needs to be passed to your frontend to join the call."
- `V2CallBase` required: `call_id`, `agent_id`, `agent_version`, `call_status`.
- `call_status` enum: `registered | not_connected | ongoing | ended | error`. A
  freshly created web call is `registered`.

**Field-placement trap (verified, oracle 9094–9157):** `from_number`, `to_number`,
`direction`, and `telephony_identifier` are required **only** in `V2PhoneCallResponse`'s
`allOf` overlay — they are **not** in the shared `V2CallBase`. A web call's conformant
body therefore **omits** all four. The `V2WebCallResponse` overlay adds only
`call_type` + `access_token`.

### 3.3 SDK round-trip target

`retell.types.WebCallResponse` is the concrete (non-Union) model; `CallResponse` is
`Union[WebCallResponse, PhoneCallResponse]` — round-trip against the concrete class
(`assert_sdk_roundtrip(payload, "retell.types:WebCallResponse")`), per the Phase 1b
lesson. On `WebCallResponse` the **required** (non-`Optional`) fields are
`call_id: str`, `agent_id: str`, `agent_version: int`, `call_status` (the 5-value
Literal), `call_type: Literal["web_call"]`, and `access_token: str`; every other
`V2CallBase` field is `Optional[...] = None` and must be omitted when null via
`response_model_exclude_none=True`. The published-agent gate (§7) guarantees a
non-null integer `agent_version`.

---

## 4. Data model — migration 0041 (owner DDL)

`Call` today has no `call_type` column; `CompatCall` hard-codes `'phone_call'`.

Add an explicit discriminator (cleaner than inferring from `sip_call_id`/
`livekit_room`, which is ambiguous for registered/undialed phone calls):

- New native enum `CallType` (Python `enum.Enum`, in `db/base.py` beside `CallStatus`/
  `CallDirection`) with members `PHONE_CALL = "phone_call"`, `WEB_CALL = "web_call"`.
- New column `calls.call_type` — `NOT NULL`, `server_default = 'phone_call'` so the
  populated table backfills and every existing phone path keeps working unchanged.
  ORM-side default `CallType.PHONE_CALL`.
- The column inherits the `calls` table's existing `usan_app` grants; the new enum
  **type** needs no grant. No new index.

Migration `0041_call_type.py`: `revision="0041"`, `down_revision="0040"`, in
`apps/api/migrations/versions/`. Upgrade: `CREATE TYPE calltype` + `ALTER TABLE calls
ADD COLUMN call_type calltype NOT NULL DEFAULT 'phone_call'`. Downgrade: drop column +
`DROP TYPE`. This is a brand-new enum type, so the Phase 1b "ALTER TYPE ADD VALUE in
the same migration" hazard does not apply. **Owner DDL** — the deploy runs alembic as
the `usan` table-owner (from the TF-managed `usan-prod-db-owner-url` secret) *before*
`compose up`, so `CREATE TYPE` / `ADD COLUMN` succeed; the runtime `usan_app` role
could not. Inert until a `v*` tag.

**`direction` stays `NOT NULL` (deliberate).** A web `Call` row stores
`direction = INBOUND` as an internal placeholder — `call_type` is the *authoritative*
discriminator, and the serializer omits `direction` from web responses (§5). We do
**not** make `calls.direction` nullable: that would touch a NOT-NULL constraint on the
core table and require auditing every `direction` reader, for no functional gain — web
rows are `REGISTERED` (the poller claims only `QUEUED`) and carry no `idempotency_key`
(never replayed), so no `direction`-keyed logic acts on them. The placeholder is
documented here so it is a conscious, reviewable choice, not a hidden smell.

---

## 5. Serialization — one uniform rule

Extend `CompatCall`: drop the hard-coded `call_type: str = "phone_call"` **default**
(`call_type: str`, always set explicitly by the serializer) and add
`access_token: str | None = None` (phone calls leave it `None` → omitted by
`response_model_exclude_none=True`).

In `serialize_call`, branch on `call.call_type`:

- **`WEB_CALL`:** set `call_type = "web_call"`; mint `access_token` via
  `mint_browser_token(settings, room=call.livekit_room,
  identity=ids.encode_call_id(call.id))`; leave `from_number`, `to_number`,
  `direction`, `telephony_identifier` as `None` (omitted) → body conforms to
  `V2WebCallResponse`. The existing `_resolve_agent` already yields the non-null int
  `agent_version` from the published profile.
- **`PHONE_CALL`:** unchanged — `call_type = "phone_call"`, `access_token = None`
  (omitted), phone fields populated as today.

Minting the token in the serializer (rather than only at create time) makes
`get-call` and `list-calls` automatically conformant for web rows, with no create-only
special case and no latent bug where a fetched web call returns a phone-shaped or
token-less body (the *altitude* principle — fix at the shared mechanism). The token is
a **bearer credential**: it is minted on demand, **never stored** in the DB, and
**never logged**. `list-calls` mints a fresh token per web row (a local HMAC sign, no
I/O); a token differing between fetches is intended (each is a fresh, narrow 15-minute
window). `mint_browser_token`'s grant (`room_join` + `can_publish` + `can_subscribe`,
scoped to exactly that room) is correct for a web caller; its docstring/comment (today
"never on the production call path") is updated when web adopts it. Cross-org
eavesdrop is not possible: the endpoints are key-gated and RLS-scoped, room names are
opaque UUIDs, and a token is only ever returned to the org that owns the call.

---

## 6. Request schema — `CreateWebCallRequest`

New Pydantic model in `apps/api/src/usan_api/compat/schemas/calls.py`:

```python
class CreateWebCallRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    agent_version: int | str | None = None
    agent_override: dict[str, Any] | None = None       # accept + persist (audit), not honored
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None
    current_node_id: str | None = None                 # accept + persist (audit), not honored
    current_state: str | None = None                   # accept + persist (audit), not honored
    model_config = ConfigDict(extra="forbid")
```

Validation failures → 422 via the global handler. As on the phone path, reject any
`retell_llm_dynamic_variables` key starting with `RESERVED_VAR_PREFIX` ("__meta") →
`CompatError(422, ...)`, so a client cannot collide with the reserved metadata /
un-honored namespaces. Per-request body size is bounded by the HTTP server, not
per-field; `agent_override` is persisted verbatim and never deserialized or executed.

---

## 7. Service — `create_web_call`

`apps/api/src/usan_api/compat/call_create.py` (alongside `create_compat_call` /
`register_compat_call`):

```
async def create_web_call(db, settings, body, *, organization_id) -> Call
```

Flow:

1. **Resolve + gate the agent** (reuse the `register_compat_call` pattern, inline —
   no helper extraction needed): `profile_id = decode_agent_id(body.agent_id)` (→ 422
   on malformed) then `if not await agent_profiles_repo.is_live_profile(db,
   profile_id): raise CompatError(422, "agent_id must reference a published agent")`.
   `agent_id` is required for web, so this is exactly the register gate — no
   published-default lookup. The serializer later derives the int `agent_version` from
   the profile's `published_version`.
2. **Pack vars.** Reject reserved-prefix dynamic-var keys (§6), then
   `packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)`.
   Persist the un-honored fields for audit **without polluting the echo**: add a second
   reserved key `packed["__meta_unhonored__"] = json.dumps({"agent_override": ...,
   "current_node_id": ..., "current_state": ...})` (only when any is non-null).
   `unpack_dynamic_vars` is extended to also `pop` `__meta_unhonored__`, so it never
   appears in the echoed `metadata` or `retell_llm_dynamic_variables`. (It shares the
   `__meta` prefix, so the §6 client-key guard already protects it.) This mirrors the
   existing `__meta__` mechanism.
3. **resolved_vars = {}.** A web call has no `Contact`, so there are **no** builtin
   vars to resolve (the contact-driven `resolve_builtin_vars` path does not apply).
   The request's dynamic vars ride as `dynamic_vars`; the worker's `build_vars` merges
   timezone-derived builtins + the custom vars. This matches the Test-Audio path,
   which also dispatches with `resolved_vars: {}`.
4. **Persist** `Call(call_type=WEB_CALL, status=REGISTERED, direction=INBOUND,
   profile_override=profile_id, dynamic_vars=packed,
   livekit_room=f"usan-web-{uuid4().hex}", contact_id=None,
   organization_id=organization_id)`; `flush()` to obtain `call_id` (the dispatch
   metadata needs it). Not committed yet.
5. **Create room + dispatch** via a **new** `dispatch_web_agent` (§8.1) — *not* the
   existing `dispatch_agent`, which gates on `outbound_configured` (Telnyx SIP) and
   would refuse a web call. `dispatch_web_agent` pre-creates the room and dispatches
   with `session_kind="call"`, `call_type="web_call"`, the `call_id`, `dynamic_vars`,
   `resolved_vars={}`, and `timezone`. Wrap it in `try/except`: on any failure,
   `await db.rollback()` (so **no** `Call` is committed — an orphan empty LiveKit room
   self-cleans on its idle timeout), log `type(exc).__name__` + `call_id` **only**
   (never `str(exc)`, the metadata, or any token — the established PHI/secret-safe
   pattern), and raise `CompatError(502, "web call dispatch failed")`.
6. **`await db.commit()`** (compat session does not autocommit — the Phase 2 lesson),
   then serialize and return.

The route handler lives on the **existing** `calls.py` router (already mounted in
`app.py` — no `app.py` change): `create_web_call(body: CreateWebCallRequest, request,
db, settings) -> CompatCall`, `POST /v2/create-web-call`, status 201,
`response_model=CompatCall`, `response_model_exclude_none=True`, mirroring
`create_phone_call`; audit via the existing `_audit(request, op, call_id)`.

---

## 8. Agent worker — web branch (`services/agent`)

### 8.1 `dispatch_web_agent` (apps/api `livekit_dispatch.py`)

Modeled on `dispatch_test_agent` (which already pre-creates a room and dispatches a
browser-served agent with **no SIP and no `outbound_configured` gate**), but for a real
persisted call:

```
async def dispatch_web_agent(*, settings, room, call_id, dynamic_vars,
                             resolved_vars, timezone) -> None
```

builds `_web_metadata(...)` = `{"session_kind": "call", "call_type": "web_call",
"call_id": call_id, "direction": "inbound", "dynamic_vars": dynamic_vars,
"resolved_vars": resolved_vars, "timezone": timezone}`, pre-creates `room`
(`CreateRoomRequest`, swallowing an already-exists), then `create_dispatch`. No SIP
participant is created — the browser joins with the minted token.

### 8.2 Worker routing + `_run_web` (`services/agent/worker.py`)

The worker already handles browser participants generically: `entrypoint(ctx)` makes no
transport assumption, `sip.*` reads live only in `_caller_phone()` on the inbound path,
and the test-session branch already waits for a participant with no SIP read (with a
regression guard test).

- **`CallMetadata`** gains `call_type: str = "phone_call"`; `parse_metadata` extracts
  `call_type = data.get("call_type") or "phone_call"`.
- **Routing:** in `entrypoint`, before the existing `session_kind=="test"` and
  `direction` branches, add `if meta.call_type == "web_call": await _run_web(...);
  return`. (Web uses `session_kind="call"`, so it would otherwise fall into the
  inbound/outbound split.)
- **`_run_web(ctx, settings, cfg, log)`** is the inline outbound block minus SIP, minus
  voicemail:
  - register `CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx,
    goodbye_message=...)`; build the session and the agent via `build_check_in_agent(cfg,
    resolved_vars=meta.resolved_vars, custom_vars=meta.dynamic_vars,
    timezone=meta.timezone)` (the same builder + vars pipeline as outbound);
  - register `register_transcript_flush`, `register_metrics_flush`,
    `_register_dynamic_vars_receiver` before `session.start` (full side-effects —
    distinct from the test branch, which skips them);
  - `await ctx.wait_for_participant()` **generically** — no `sip.*` read, no
    `start_inbound_call`, no `_caller_phone`;
  - arm `_arm_crisis_safety_net` + `_max_duration_guard`, say the recording disclosure,
    `start_call_recording`, then proceed directly to the initial agent turn
    (`generate_reply`/greet) — **no** `_run_detection_window` (voicemail is outbound-
    only and meaningless for a browser; skipped by omission, not removed from shared
    code);
  - the call outcome persists via the registered `CheckInData`/`report_end_call` path,
    identical to inbound/outbound.

The dynamic-var receiver, crisis watcher, and max-duration guard are already
transport-agnostic (verified) and are reused unchanged.

---

## 9. Conformance & tests

- **Freeze test** `apps/api/tests/compat/test_freeze_web_calls.py`:
  - `create-web-call` happy path → 201; body `assert_conforms(payload,
    "V2WebCallResponse")` + `assert_sdk_roundtrip(payload, "retell.types:WebCallResponse")`.
  - asserts `call_type == "web_call"`, `access_token` present and non-empty,
    `call_status == "registered"`, and the **absence** of `from_number`/`to_number`/
    `direction`/`telephony_identifier`.
  - 422 on malformed `agent_id`; 422 on an unpublished/absent agent; 401 without a key;
    422 on a reserved-prefix dynamic-var key.
  - heavier optional fields accepted (201, no error); `metadata` and
    `retell_llm_dynamic_variables` round-trip **exactly** on a follow-up `get-call`
    (the `__meta_unhonored__` blob never leaks into the echo).
  - **Cross-endpoint conformance:** create a web call, fetch it via `get-call`
    (`include_transcript=True`) → `assert_conforms("V2WebCallResponse")`; fetch via
    `list-calls` (`include_transcript=False`) → same conformance (the access_token may
    differ between fetches; all other fields identical; phone fields absent in both).
  - `serialize_call` on a web row honors `include_transcript`/`include_recording`
    (False → those fields omitted, matching list-calls' light path).
  - The LiveKit dispatch (`dispatch_web_agent`) is mocked; the test asserts a real JWT
    is returned and that it is never present in any captured log line.
- **Surface coverage:** remove `('POST', '/v2/create-web-call')` from `_UNSUPPORTED`
  in `compat/routers/unsupported.py`, and from **both** verified 501-coverage files —
  `apps/api/tests/compat/test_surface_coverage.py` and
  `apps/api/tests/test_compat_fidelity.py` (the latter lists it at line 119; the Phase
  2 two-files lesson). KNOWN_GAPS stays `frozenset()`; the served-or-501 exact-path
  gate stays green.
- **Worker:** `services/agent/tests/test_web_session.py` mirroring
  `test_test_session.py` — a browser participant, no `sip.*` read, no voicemail, with
  side-effects enabled; plus a no-SIP-read regression guard for the web branch, and a
  guard that a `phone_call`/no-`call_type` job is **not** routed to `_run_web`.
- **Phone regression:** existing call freeze/fidelity tests stay green (serializer
  phone branch unchanged; `call_type` server-default backfills; `unpack` change is an
  additive `pop` of an absent key for phone rows).

---

## 10. House conventions carried forward

- Not-found → `CompatError(404)` (oracle uses 422 here; recorded deviation).
- Validation → 422 via the global handler; envelope `{"status": <int>, "message": str}`.
- `response_model_exclude_none=True` on the route; null optionals omitted.
- Compat session is not autocommit → explicit `await db.commit()` after the mutation.
- PHI/secret-safe logging: error paths log `type(exc).__name__`, never `str(exc)`,
  request/response bodies, metadata, or tokens.
- CI mypy is `uv run mypy` (config `files=["src"]`) — never `mypy .`.

---

## 11. Deviations & documented limitations

1. **Browser-side interop caveat (new).** The REST contract and the minted
   `access_token` are fully conformant, and the token is real. But the token carries
   only the room + identity, **not** the LiveKit server URL — the URL is implicit in
   the client's WebRTC config. RetellAI's `RetellWebClient` connects to *RetellAI's*
   LiveKit cloud by default, so a true zero-change repoint of the *browser* requires the
   client's frontend to target our `LIVEKIT_URL` (e.g. raw `livekit-client` +
   our token, or an SDK build that accepts a custom server URL). Same class as the
   outbound-webhook edge caveat. Documented in `docs/deployment/web-calls-livekit-url.md`.
2. **`agent_override` / `current_node_id` / `current_state` accepted, persisted for
   audit under a reserved never-echoed key, not honored** (§2.2). Honoring is Phase 6.
3. **404-not-found and int-status-envelope** surface-wide deviations carried from
   prior phases (a future surface-wide conformance pass owns these).

---

## 12. Out of scope (explicit)

- `V3WebCallResponse` / `/v3` web-call surface (the oracle's `/v2/create-web-call`
  returns `V2WebCallResponse`; the v3 list filter `call_type` already works through the
  existing list endpoint once rows carry `call_type`).
- Honoring `agent_override` / conversation-flow node entry (Phase 6 territory).
- A separate web-call default-agent profile — web requires an explicit published
  `agent_id`, like `register-phone-call`.

---

## 13. Task breakdown (TDD tasks)

1. **Data model** — `CallType` enum + `Call.call_type` column + migration 0041
   (+ a phone-path regression guard; document the `direction=INBOUND` placeholder).
2. **Serializer** — `CompatCall.call_type` (no default) + `access_token` field;
   `serialize_call` web branch (mint token, omit phone fields) + serializer unit tests;
   `unpack_dynamic_vars` strips `__meta_unhonored__`.
3. **Request schema** — `CreateWebCallRequest` (+ reserved-prefix key guard) + unit
   tests.
4. **Service + dispatch** — `dispatch_web_agent` (+ `_web_metadata`) in
   `livekit_dispatch.py`; `create_web_call` service (resolve → pack + audit-stash →
   persist → room+dispatch → commit; PHI-safe 502 on failure) + service unit tests
   (LiveKit mocked).
5. **Router + surface** — `POST /v2/create-web-call` on the existing calls router;
   remove from `_UNSUPPORTED` + both 501 files; `test_freeze_web_calls.py`
   (incl. the get-call/list-calls cross-endpoint conformance + no-token-in-logs checks).
6. **Worker** — `CallMetadata.call_type` + `parse_metadata`; `entrypoint` routing;
   `_run_web` branch; `services/agent/tests/test_web_session.py`.
7. **Docs** — `docs/deployment/web-calls-livekit-url.md` (the browser-interop caveat).

The subagent-driven-development final whole-branch review runs as the process gate
after Task 7 (not a numbered task). Ends at squash-merge to `main`. **No `v*` tag** —
Phase 3 stays inert until an operator deploys migration 0041 and the new worker code.
