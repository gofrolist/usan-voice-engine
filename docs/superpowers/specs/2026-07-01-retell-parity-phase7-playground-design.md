# Phase 7 (slice 1) — `agent-playground-completion` Design

**Status:** Approved 2026-07-01.
**Program:** RetellAI full-parity ([roadmap](2026-06-24-retell-full-parity-program-roadmap.md), Phase 7 "long tail" — Playground pulled forward as the cheapest sub-item).
**Scope:** Serve the single oracle op `POST /agent-playground-completion/{agent_id}` as a LIVE, stateless single-turn Vertex completion. Promotes the op out of the 501 router.

## 1. Goal

Any RetellAI client calling `retell.playground.completion(agent_id=..., messages=[...])` gets a real agent reply from our engine, conformant to the pinned oracle, with zero code changes on their side. This is the thinnest greenfield Phase-7 slice: a text-only single-turn wrapper over the Vertex path we already run for chat.

## 2. Non-goals (accepted-and-ignored, documented POSTURE)

The oracle request carries advanced optional fields for a full playground simulator. This slice **accepts them without acting on them** and omits the response fields it does not produce:

| Field | Handling |
|-------|----------|
| `?version` (query) | accepted, ignored — always serve the currently-**published** config (matches `get-chat-agent`, which accepts `?version` and serves the published view; int+tag version resolution deferred) |
| `tool_mocks` | accepted, ignored (single `tools=[]` turn — no tool loop) |
| `current_state` | accepted, ignored (no retell-llm state machine in playground) |
| `current_node_id` / `component_id` | accepted, ignored (no conversation-flow execution in playground) |
| response `current_state` / `current_node_id` | omitted (`exclude_none`) |
| response `dynamic_variables` | omitted (`exclude_none`) |
| response `call_ended` | omitted (`exclude_none`) |
| response `knowledge_base_retrieved_contents` | omitted — **no RAG** in playground this slice |

Honored request fields: `messages` (required, full history) and `dynamic_variables` (prompt substitution only). Rationale: mirrors `chat_service.generate_agent_reply` (`tools=[]`), keeps blast radius minimal, and the omitted response fields are all optional-and-omit-when-null in the oracle (`exclude_none` → conformant). Tool-mocks, flow-resume, and KB retrieval in playground are deferred to later Phase-7 slices if a client needs them.

## 3. Oracle surface (pinned `openapi-final.yaml`)

- **Method + path:** `POST /agent-playground-completion/{agent_id}` (`operationId: agentPlaygroundCompletion`).
- **Params:** `agent_id` path (required, string); `version` query (optional, `AgentVersionReference` = int ≥ 0 OR tag string; "Defaults to latest").
- **Request body** (`required: true`): required `messages: array<ChatMessageInput>`; optional `dynamic_variables: object<string,string>`, `tool_mocks: array<ToolMock>`, `current_state: string`, `current_node_id: string`, `component_id: string`. `ChatMessageInput` is a `oneOf` whose `MessageBase` variant requires `role` (enum `agent|user`) + `content`, optional `message_id`.
- **Response 200** (inline `type: object`, **not** a named component): required `messages: array<MessageOrToolCall>` ("New messages … Does not include the input messages"); optional `current_state`, `current_node_id`, `dynamic_variables`, `call_ended: boolean`, `knowledge_base_retrieved_contents: array<string>`.
- **SDK types:** request `retell.types.playground_completion_params:PlaygroundCompletionParams`; response `retell.types.playground_completion_response:PlaygroundCompletionResponse` (pydantic; `messages: List[Message]` required, all others `Optional[...] = None`). Resource method: `retell.playground.completion(...)`.

## 4. Architecture

Dedicated router + thin service, matching every other served compat op group. Reuses the in-`apps/api` Vertex path end-to-end. **No `services/agent` involvement** (Constitution I holds trivially — playground is HTTP-in / Vertex-out, no telephony).

```
POST /agent-playground-completion/{agent_id}?version
        |
        v
compat/routers/playground.py   (decode id, Query(version), exclude_none dump)
        |
        v
compat/playground_service.py::run_playground_completion(db, settings, *, agent_id, version, request)
        |  resolve org-scoped published AgentConfig (RLS)
        |  build system_instruction (substitute + build_vars)
        |  map messages -> genai contents (agent->model / user->user)
        |  503 gate on gcp_project
        v
vertex_test.run_vertex_turn(model, temperature, system_instruction, tools=[], contents, settings)
        |
        v
PlaygroundCompletionResponse{messages:[{message_id, role:"agent", content, created_timestamp}]}
```

### Components

- **`compat/schemas/playground.py`** (new):
  - `PlaygroundMessageInput` — `role: Literal["agent","user"]`, `content: str`, optional `message_id: str | None`; `model_config = ConfigDict(extra="allow")` so the other six `ChatMessageInput` `oneOf` variants (tool-call / transition / injected / sms) deserialize without error and are simply not mapped into `contents`.
  - `PlaygroundCompletionRequest` — `messages: list[PlaygroundMessageInput]` (required, non-empty enforced by a validator → 422 if empty), `dynamic_variables: dict[str, str] | None = None`, `tool_mocks: list[Any] | None = None`, `current_state: str | None = None`, `current_node_id: str | None = None`, `component_id: str | None = None`. `extra="allow"`.
  - `PlaygroundMessageOut` — `message_id: str`, `role: Literal["agent"]`, `content: str`, `created_timestamp: int`.
  - `PlaygroundCompletionResponse` — `messages: list[PlaygroundMessageOut]`; optional `current_state / current_node_id / dynamic_variables / call_ended / knowledge_base_retrieved_contents` all defaulting `None` (present for future slices, always omitted now via `exclude_none`).
- **`compat/playground_service.py`** (new): `async def run_playground_completion(db, settings, *, agent_id: str, version: str | None, request: PlaygroundCompletionRequest) -> PlaygroundCompletionResponse`.
- **`compat/routers/playground.py`** (new): `@router.post("/agent-playground-completion/{agent_id}")` with `agent_id: str` path, `version: str | None = Query(default=None)`, `body: PlaygroundCompletionRequest`, deps `db=Depends(get_compat_db)`, `settings=Depends(get_settings)`, `request: Request`. No `response_model` (matches `chat_agents.py`); returns `result.model_dump(exclude_none=True)`.
- **`compat/routers/unsupported.py`**: remove the `("POST", "/agent-playground-completion/{agent_id}")` tuple.
- **App wiring:** include the new router on the compat sub-app next to `chat_agents`.
- **Both** surface-coverage tests updated: `tests/compat/test_surface_coverage.py` **and** `tests/test_compat_fidelity.py` (the op moves 501 → served).

## 5. Data flow (per request)

1. **Resolve agent + config (RLS-scoped).** Decode `agent_id` via `ids.decode_agent_id` (malformed → `CompatError(422, "invalid agent_id")`, already raised inside the codec); resolve the org-scoped **published** config through `agent_profiles_repo.get_profile` → `get_published_config` → `AgentConfig.model_validate(version.config)`, mirroring `chat_service._load_published_config`. Unknown id, cross-org (RLS-filtered), or no published version → **422** (`CompatError(422, "agent is not available")`) — indistinguishable whether cross-org or unpublished; never leaks existence across orgs. **NOTE:** the oracle declares this op's error responses as 400/401/402/422/429/500 — there is **no 404**, so "not found" is realized as the conformant **422**, matching the existing chat path. `?version` is accepted and ignored (currently-published config served). Channel-agnostic: resolve regardless of the `channel` overlay (playground works on any agent).
2. **Build system prompt.** `values = build_vars({}, request.dynamic_variables or {}, timezone="", now=datetime.now(UTC))`; `system_instruction = substitute(cfg.prompts.system_prompt, values)`. `timezone=""` is correct here — this is an internal system prompt whose output is model text, never a recipient-facing template (the 4b-2 distinction).
3. **Map history → contents.** For each `PlaygroundMessageInput` with role in {agent,user}: `{"role": "model" if role == "agent" else "user", "parts": [{"text": content}]}` — the same mapping `flow_runtime.history_to_contents` uses. Non-`MessageBase` variants (no `content`) are skipped. A small adapter (list comprehension) — do **not** force the playground schema through the `CompatChatMessage` type just to reuse the helper.
4. **503 gate.** `if not settings.gcp_project: raise CompatError(503, "playground completion unavailable")` — after step-2/3 validation, before the Vertex call. Placed like `chat_service.py:341` / `admin_profile_tests.py:135`.
5. **Vertex turn.** `turn = await run_vertex_turn(model=cfg.llm.model, temperature=cfg.llm.temperature, system_instruction=system_instruction, tools=[], contents=contents, settings=settings)`.
6. **Response.** One output message: `PlaygroundMessageOut(message_id=str(uuid4()), role="agent", content=turn.text, created_timestamp=<int epoch ms>)`. `turn.text` may be `""` (still a conformant message). `message_id` + `created_timestamp` are included because the SDK `Message` variant round-trips them and they are cheap; all other response fields stay `None` → omitted.

## 6. Error handling

Ordering inside `run_playground_completion` and the router:
1. **422** — pydantic validation of the body (missing/empty `messages`) at FastAPI parse time; and `ids.decode_agent_id` on a malformed id → `CompatError(422, "invalid agent_id")`.
2. **422** — no org-scoped published config resolvable (unknown / cross-org / unpublished) → `CompatError(422, "agent is not available")`. (The oracle declares no 404 for this op.)
3. **503** — `settings.gcp_project` unset → `CompatError(503, "playground completion unavailable")`.
4. Vertex call wrapped: `except CompatError: raise` **before** `except Exception as exc: logger.bind(err=type(exc).__name__).error(...); raise CompatError(502, "playground completion failed") from None`.

Nothing is persisted or sent, so there is **no DB write to roll back** and no `db.commit()`. Only `type(exc).__name__` is logged — never message content, dynamic vars, or the instruction (PHI containment, Constitution II). `except CompatError: raise` must precede the broad handler so 422/503 are not swallowed into 502.

## 7. Conformance & testing

- **Primary anchor:** `assert_sdk_roundtrip(payload, "retell.types:PlaygroundCompletionResponse")` — the oracle 200 body is inline (no named component), so the SDK model governs the whole-response shape (and validates each `messages` item as `Message`). Each emitted message is additionally checked against the oracle via `assert_conforms(msg, "MessageOrToolCall")` (the exact `$ref` the response array uses). Marked `@pytest.mark.frozen`.
- **Service-level tests** (`tests/compat/test_playground_service.py`), seeding a published agent inline via the superuser `async_database_url` engine (copy the `create_chat_completion` service-test seed; `run_vertex_turn` mocked):
  - happy path — single user turn → one `role="agent"` message with the mocked text; `message_id` + `created_timestamp` present.
  - multi-turn history maps agent→model / user→user in order.
  - `dynamic_variables` substituted into the system prompt (assert on the `system_instruction` passed to the mocked `run_vertex_turn`).
  - advanced fields present (`tool_mocks`, `current_state`, `current_node_id`) → still exactly one plain agent message; none echoed.
  - `exclude_none` — response dict has no `current_state / current_node_id / dynamic_variables / call_ended / knowledge_base_retrieved_contents` keys.
  - 422 — unknown id; cross-org id (seed a second org's agent, set tenant context to a different org, assert 422 "agent is not available" not 200); malformed id → 422 "invalid agent_id".
  - 503 — `gcp_project` unset.
- **Router/endpoint tests** (`tests/compat/test_playground_endpoint.py`) via the compat client: 200 happy path (mock the service or `run_vertex_turn`), 422 empty `messages`, 401 without a compat key.
- **Surface coverage:** update `tests/compat/test_surface_coverage.py` and `tests/test_compat_fidelity.py`; `KNOWN_GAPS` stays `frozenset()`.

## 8. Deployment posture

Ships **inert** with no new machinery: no migration, no new env key, no new dependency. Activation = the existing compat-key auth (401 until a super-admin mints a key) **and** `settings.gcp_project` set (503 until then). Documented in `docs/deployment/playground-completion.md` (one short page: what it does, the two gates, the accepted-and-ignored POSTURE table). Not deployed until a `v*` tag, like every prior phase.

## 9. Constitution / program conventions honored

- **I (no cross-app import):** trivially — no `services/agent` touch.
- **II (PHI containment):** Vertex via `run_vertex_turn` (ADC, `vertexai=True`) only; logs `type(exc).__name__` + counts only; nothing persisted.
- **RLS isolation:** cross-org/unknown agent → 422 "agent is not available", indistinguishable whether cross-org or unpublished (oracle declares no 404 for this op).
- **Oracle governs:** SDK round-trip is the conformance gate; `exclude_none` omit-when-null.
- **Ships inert; squash-merge only on explicit go-ahead; no `v*` tag without go-ahead.**
