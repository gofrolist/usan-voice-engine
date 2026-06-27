# RetellAI Parity Phase 4b-1 — `create-sms-chat` (compat op) — Design

**Status:** approved (design), 2026-06-26
**Program:** RetellAI full-API-parity (any client repoints base URL with zero changes)
**Predecessor:** Phase 4a (api_chat sessions, live via Vertex) — MERGED squash `2a936fa` (#141)
**This phase ships as its own squash-merged PR. No `v*` tag (inert until an operator deploys migration 0043 + enables Telnyx messaging).**

---

## 1. Goal

Serve RetellAI's **only client-facing SMS operation**, `POST /create-sms-chat`, on the compat
sub-app: persist an `sms_chat`-typed chat session, generate its initial SMS message from the
agent's configured greeting, send it via Telnyx, and return a conformant `ChatResponse`
(`chat_type: sms_chat`). After this phase, `KNOWN_GAPS` stays empty and the only remaining
`# --- Chat ---` 501 entry (`POST /create-sms-chat`) is served.

## 2. Scope

**In scope (4b-1):**
- `POST /create-sms-chat` → service + router + request schema.
- Additive migration `0043`: nullable `from_number` / `to_number` on `chat_sessions`.
- Live initial-message send via the existing `telnyx_messaging.send_sms` (gated; inert when unconfigured).
- A guard so `create-chat-completion` rejects an `sms_chat` session.

**Out of scope → deferred to Phase 4b-2 (inbound two-way engine):**
- The inbound Telnyx Messaging webhook reply loop (parse inbound `to`, match the open
  `sms_chat`, run a Vertex reply, send it back), per-message Telnyx-id dedup
  (`telnyx_message_id` on `chat_messages`), and the `/webhooks/telnyx` route branch.
- Multi-tenant cross-org inbound routing (the existing inbound plane is single-org / default-org).
- Weighted binding selection and multi-number sending.

**Why this cut:** the oracle exposes exactly one SMS op to API clients (`create-sms-chat`); the
inbound reply loop is RetellAI-internal and invisible to the API (clients observe it only as a
growing `get-chat` transcript). So `create-sms-chat` is the entire conformance deliverable, and
the inbound engine — which carries the multi-tenant org-routing, live-Telnyx, PHI-webhook, and
family-care-route-coupling complexity — is a clean, independently-shippable follow-up.

## 3. Oracle contract (vendored `apps/api/tests/compat/oracle/openapi-final.yaml` v3.0.0)

### 3.1 `create-sms-chat` (lines 11052–11160)
- **Path / method / operationId:** `POST /create-sms-chat` / `createSmsChat`.
- **requestBody:** `required: true`, `application/json`, **inline `type: object`** (no named
  `CreateSmsChatRequest` component).
  - **Required:** `from_number` (string, `minLength:1`, E.164 — "a number purchased from Retell
    or imported to Retell with SMS capability"), `to_number` (string, `minLength:1`, E.164).
  - **Optional:** `override_agent_id` (string, `minLength:1` — "one time override", does not bind
    the agent to the number); `override_agent_version` (`$ref AgentVersionReference`);
    `metadata` (free-form object, storage-only, echoed back); `retell_llm_dynamic_variables`
    (object, `additionalProperties: {type: string}`).
- **Response:** **`200`** (NOT 201) → `$ref '#/components/schemas/ChatResponse'`, description
  "SMS chat created and initial message sent successfully". Errors 400/401/402/422/429/500 use
  the shared response components.
- **Op description:** "Start an outbound SMS chat conversation … The agent must be configured for
  chat mode. The initial SMS message will be automatically generated and sent based on the
  agent's configuration."

### 3.2 `ChatResponse` (lines 2408–2534) — the SAME component as api_chat
- **Required:** `chat_id`, `agent_id`, `chat_status` (enum `ongoing|ended|error`). Everything
  else optional.
- `chat_type` enum is `api_chat | sms_chat` — an SMS chat returns `chat_type: sms_chat`.
- `V3ChatResponse` (used only by `/v3/list-chats`) is `allOf[ChatResponse, {not: required
  transcript / message_with_tool_calls / scrubbed_message_with_tool_calls}]` → list items still
  must omit those keys. The Phase 4a `exclude_none` + `serialize_chat(include_transcript=False)`
  machinery already satisfies this for sms_chat rows.

### 3.3 No other SMS surface
`create-sms-chat` is the **only** SMS path/operation in the entire oracle. There is **no**
inbound-message op, **no** append-sms-message op, **no** sms-status op, and **no**
`SmsChatResponse` / `CreateSmsChatRequest` named component. Inbound SMS content surfaces to the
client only as read-only transcript data (`SmsMessage`/`SmsUtterance` inside
`message_with_tool_calls`) via `get-chat`. The only inbound "hook" in the oracle is the
phone-number config field `inbound_sms_webhook_url` (Retell calls outward to the client) — out
of scope here.

### 3.4 SDK round-trip pin (`retell-sdk==5.53.0`)
- `retell.chat.create_sms_chat(*, from_number: str, to_number: str, metadata=…,
  override_agent_id=…, override_agent_version: int|str =…, retell_llm_dynamic_variables=…) ->
  ChatResponse` — POSTs to `/create-sms-chat`, **`cast_to=ChatResponse`** (the same concrete
  `retell.types.chat_response:ChatResponse` used by create/get/list/end api_chat — NOT the
  api_chat-only `ChatCreateChatCompletionResponse`).
- Conformance tests deserialize the response into `retell.types:ChatResponse`.

## 4. Data model — migration `0043`

Additive, `revision="0043"`, `down_revision="0042"`. Add two **nullable** columns to
`chat_sessions`:

| column | type | null | meaning |
|---|---|---|---|
| `from_number` | `TEXT` | yes | E.164 sender (our provisioned Telnyx number). Null for api_chat rows. |
| `to_number` | `TEXT` | yes | E.164 recipient. Null for api_chat rows. |

- `chat_type` (existing `TEXT`, default `'api_chat'`) takes the value `'sms_chat'` for SMS rows.
- **No** `telnyx_message_id` column on `chat_messages` in this phase — that is inbound-dedup,
  deferred to 4b-2.
- Columns are nullable → cheap add, api_chat rows unaffected, RLS already in force on the table
  (no new policy needed).
- **Owner-DDL migration** (per the migrations-need-owner convention): the deploy migrates as the
  `usan` owner before `compose up`.

ORM (`db/models.py`, `ChatSession`): add `from_number: Mapped[str | None]` and
`to_number: Mapped[str | None]` (both `mapped_column(Text, nullable=True)`).

## 5. Telnyx send path (reused, unchanged)

`telnyx_messaging.send_sms(settings: Settings, *, to_number: str, body: str) -> str` (returns the
Telnyx message id; raises `TelnyxMessagingError` on any failure). Reads
`telnyx_messaging_api_key` / `telnyx_messaging_profile_id` / `telnyx_from_number` /
`telnyx_messaging_api_url` / `telnyx_messaging_timeout_s`. It always sends **from**
`settings.telnyx_from_number` (the single provisioned number) — so the request's `from_number`
must equal that configured sender (see §6 step 2). `send_sms` is **not modified** in this phase.

`telnyx_messaging_enabled` (default **False**) gates the whole feature; the 3 messaging secrets
default unset. All already present in compose `environment:` + `.env` (ship-inert).

## 6. Service layer — `compat/chat_service.py :: create_sms_chat`

`async def create_sms_chat(db, settings, body: CreateSmsChatRequest) -> tuple[ChatSession,
list[ChatMessage]]` (returns the session + its single initial message for the router to
serialize). **Ordering is chosen for PHI/rollback safety — the config gate fires before any
PHI write, and any send failure rolls back the whole transaction so no half-written exchange or
orphan row survives.**

1. **503 — sending not ready, before any write.** `if not _sms_send_ready(settings): raise
   CompatError(503, "sms messaging is not configured")`. `_sms_send_ready(settings) -> bool` =
   `telnyx_messaging_enabled` true **and** `telnyx_messaging_api_key` / `telnyx_messaging_profile_id`
   / `telnyx_from_number` all set. (Mirrors Phase 4a's 503-before-PHI gate; no `gcp_project`
   needed — no Vertex in 4b-1.)
2. **422 — `from_number` must be our provisioned sender.** `if body.from_number !=
   settings.telnyx_from_number: raise CompatError(422, "from_number is not a provisioned
   sender")`. (Multi-number sending deferred.)
3. **Resolve the agent (422 if none):**
   - if `body.override_agent_id`: `profile_id = ids.decode_agent_id(override_agent_id)`;
     require `is_live_profile` else `CompatError(422, "invalid agent_id")`.
   - **else** honor the `from_number` binding **within the caller's org** (RLS-safe, same-org —
     not the deferred cross-org inbound case): `pn = phone_numbers_repo.get_by_e164(db,
     body.from_number)`; take `pn.outbound_sms_agents[0]["agent_id"]`, decode + `is_live_profile`;
     if absent/empty → `CompatError(422, "no agent bound to from_number")`. This is the first op
     to honor a number→agent binding (outbound SMS, same-org); `override_agent_id` always wins.
   - `agent_version` resolved like Phase 4a (published version of the profile).
4. **Build the initial message from the agent's greeting.** Load the published `AgentConfig`
   (`agent_profiles_repo.get_published_config` → `AgentConfig.model_validate(version.config)`, as
   in Phase 4a's `_load_published_config`). Substitute the caller's
   `retell_llm_dynamic_variables` into `config.prompts.greeting`:
   `vars_ = build_vars({}, body.retell_llm_dynamic_variables or {}, timezone="",
   now=datetime.now(UTC))`; `greeting = substitute(config.prompts.greeting, vars_)`.
   (`substitute` never raises; unknown `{{slots}}` are handled.)
5. **Persist (flush, not commit):** `add_session(db, agent_profile_id=…, agent_version=…,
   dynamic_vars=pack_dynamic_vars(body.retell_llm_dynamic_variables or {}, body.metadata or {}),
   chat_type="sms_chat", from_number=body.from_number, to_number=body.to_number)`
   (Phase 4a's `add_session` is parameterized to accept `chat_type` + the numbers, defaulting to
   `'api_chat'`/`None`). Then `add_message(db, session_id=…, seq=1, role="agent",
   content=greeting)`. `await db.flush()`.
6. **Send via Telnyx, roll back the whole txn on failure:**
   ```
   try:
       await telnyx_messaging.send_sms(settings, to_number=body.to_number, body=greeting)
   except CompatError:
       raise
   except Exception as exc:            # incl. TelnyxMessagingError
       await db.rollback()
       logger.warning("create_sms_chat send failed: {err}", err=type(exc).__name__)
       raise CompatError(502, "sms send failed") from None
   ```
   Never log `to_number`, the greeting body, or the dynamic vars.
7. **Commit + return** `(session, [message])`. The router serializes with
   `serialize_chat(session, [message], include_transcript=True)` so the response shows the sent
   opener; `chat_status` is `ongoing`, `chat_type` is `sms_chat` (the serializer already emits
   `session.chat_type`).

**Residual orphan window (documented, accepted):** if `send_sms` succeeds but the subsequent
commit fails, an SMS was sent with no persisted row. The window is tiny (commit after a clean
flush) and mitigation (idempotency key) is deferred — noted in the deployment doc.

## 7. Request/response schemas — `compat/schemas/chats.py`

Add `CreateSmsChatRequest(BaseModel)` with `model_config = ConfigDict(extra="forbid")`:

```python
class CreateSmsChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_number: str = Field(min_length=1)
    to_number: str = Field(min_length=1)
    override_agent_id: str | None = Field(default=None, min_length=1)
    override_agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None
```

Response reuses the existing `CompatChat` (already emits `chat_type` from the column and omits
empty fields under `response_model_exclude_none=True`). No new response model.

## 8. Reuse — what works with no new code

`get-chat`, `POST /v3/list-chats`, `update-chat`, `end-chat`, `delete-chat` already query
`chat_sessions` chat-type-agnostically and serialize via `serialize_chat`, which emits
`session.chat_type` — so an `sms_chat` row created here is immediately gettable, listable,
updatable, endable, and (soft-)deletable, all conformant, with **zero** changes to those paths.
The `chat_`/`message_` id codecs and the keyset pagination are unchanged.

## 9. Cross-cutting guard — `create-chat-completion` rejects `sms_chat`

`create_chat_completion` must reject a session whose `chat_type == 'sms_chat'` with
`CompatError(422, "cannot complete an sms chat")` (after `lock_session`, before any write). SMS
replies are webhook-driven (4b-2), never synchronously injected through the api_chat completion
endpoint.

## 10. Router — `compat/routers/chats.py`

Add one route (mirrors Phase 4a route style): `@router.post("/create-sms-chat",
response_model=CompatChat, response_model_exclude_none=True)`; params
`body: CreateSmsChatRequest, request: Request, db=Depends(get_compat_db),
settings=Depends(get_settings)`; calls `chat_service.create_sms_chat`, serializes with
`include_transcript=True`, then `_audit(request, "create-sms-chat", chat_id)` (org/op/chat_id
only — never the numbers). Status code defaults to **200** (the oracle uses 200, not 201).

Remove `("POST", "/create-sms-chat")` from `_UNSUPPORTED` in `routers/unsupported.py` (the only
`# --- Chat ---` 501 entry).

## 11. Config / deployment posture

- Inert until an operator sets `telnyx_messaging_enabled=true` + the 3 Telnyx messaging secrets
  (api_key / profile_id / from_number). Until then `create-sms-chat` returns **503**.
- The 6 other compat keys still 401 until a super-admin mints a compat key.
- Migration `0043` runs as the `usan` owner before `compose up`.
- **No `v*` tag** in this PR — merge to main only; an operator deploys later.
- No `gcp_project` needed for 4b-1 (Vertex enters in 4b-2).

## 12. Security / PHI

- No inbound webhook in 4b-1 → no new Ed25519/replay surface.
- Never log `to_number`, the greeting body, or dynamic vars. Send-failure log is
  `type(exc).__name__` only, `raise … from None`.
- `_audit` carries only org / op / `chat_id`.
- Whole-transaction rollback on any send failure (no half-written PHI exchange, no orphan row).
- RLS `organization_id` stays the DB server-default (`COALESCE(current_setting('app.current_org',
  true)::uuid, default_org_id())`), never set by app code. The `from_number` binding lookup runs
  under the caller's own org context (same-org, RLS-safe).

## 13. Testing & conformance

- **Surface coverage:** remove the `_UNSUPPORTED` entry + mount the route → both
  `tests/compat/test_surface_coverage.py` checks (served-or-501-or-gap with empty `KNOWN_GAPS`;
  501-paths-match-oracle) stay green automatically. `tests/test_compat_fidelity.py` does not list
  `/create-sms-chat`, so no forced edit there; verify it still passes.
- **New `tests/compat/test_freeze_sms_chat.py`:**
  - SDK round-trip: `retell.chat.create_sms_chat(from_number=<sender>, to_number=…)` deserializes
    to `retell.types:ChatResponse` with `chat_status='ongoing'`, `chat_type='sms_chat'`, a
    `chat_id` that decodes, and the greeting present in the transcript.
  - `503` when `telnyx_messaging_enabled` off (gcp fixture irrelevant); fixtures flip the flag +
    secrets via `dependency_overrides` on the compat app.
  - `422` on a `from_number` that isn't the provisioned sender; `422` on a `to_number`/agent with
    no resolvable agent; `override_agent_id` precedence over the binding.
  - The `sms_chat` row is gettable via `get-chat` and appears in `/v3/list-chats` (with
    `transcript`/`message_with_tool_calls` omitted on the list item).
  - `create-chat-completion` on the new `sms_chat` chat_id → `422`.
  - `telnyx_messaging.send_sms` is mocked (assert called with `to_number` + greeting body); a
    `TelnyxMessagingError` from the mock → `502` and **no** persisted row (rollback proven by a
    follow-up `get-chat` → 404 / list count unchanged).
- Repo/serializer unit tests: `add_session` with `chat_type='sms_chat'` + numbers persists and
  round-trips; `serialize_chat` emits `chat_type='sms_chat'`.

## 14. Deviations & deferred (documented)

- **4b-2 (next phase):** inbound two-way SMS engine — parse the inbound `to`, match the open
  `sms_chat` by `from_number` under **default-org** scope, run a Vertex reply (reusing the Phase
  4a completion path), send it via `send_sms`, dedup on a new `telnyx_message_id`
  (`chat_messages`), and branch `/webhooks/telnyx` (keeping SMS chat out of the family-task/DNC
  path).
- **Multi-tenant cross-org inbound routing** (resolve org from the inbound `to` number via a
  SECURITY DEFINER lookup): deferred; the inbound plane stays single-org, as today.
- **Weighted binding selection** (the binding lists carry `weight`): 4b-1 takes the first
  `outbound_sms_agents` entry; weighted/random selection deferred.
- **Multi-number sending:** 4b-1 requires `from_number == settings.telnyx_from_number`; sending
  from multiple provisioned numbers deferred.
- **Honoring the outbound binding** is new behavior for create-sms-chat (same-org, outbound only)
  and does not change the still-deferred call-routing binding behavior.

## 15. Task breakdown (≈6 TDD tasks → subagent-driven development)

1. **Migration 0043 + ORM columns** — `from_number`/`to_number` nullable on `chat_sessions`
   (+ model `Mapped[str|None]`). Test: upgrade/downgrade + an `sms_chat` row persists the numbers.
2. **`CreateSmsChatRequest` schema + `_sms_send_ready` helper + parameterized `add_session`**
   (`chat_type`/`from_number`/`to_number`, defaulting to `'api_chat'`/`None`). Tests: schema
   `extra="forbid"`, helper truth table, repo round-trip.
3. **Agent-resolution + greeting helpers** in `chat_service` (override → same-org binding → 422;
   greeting substitution). Tests with a seeded published profile + a `PhoneNumber` binding.
4. **`create_sms_chat` service** — the §6 ordered flow (503-first, 422 gates, flush, send,
   502-rollback, commit). Tests with `send_sms` mocked incl. the rollback path.
5. **Router route + remove the 501 entry + `create-chat-completion` sms guard.** Tests: 200
   happy path, `_audit` PHI-free, surface coverage green, completion 422 on sms_chat.
6. **`test_freeze_sms_chat.py` conformance suite + `docs/deployment/sms-chat.md`** (operator
   note: migration 0043 owner-DDL, the Telnyx flags, 503-when-off, the orphan-window caveat,
   the deferred 4b-2 inbound engine).

---

**Global constraints (bind every task):** oracle is ground truth (200 not 201; `ChatResponse`
shape; `chat_type='sms_chat'`); `exclude_none` omits null optionals; PHI-safe logging
(`type(exc).__name__` only, never numbers/body/vars, `raise … from None`); 503-before-any-write;
502 whole-txn rollback on send failure; RLS `organization_id` is the DB server-default, never
app-set; `send_sms` unchanged; `apps/api` must not import `services/agent`; squash-merge to main,
**no `v*` tag**.
