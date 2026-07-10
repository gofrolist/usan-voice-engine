# Surface 3 — External HTTP Agent Tools (design)

**Date:** 2026-07-09 · **Status:** Draft for review · **Program:** RetellAI full-parity (roadmap `2026-06-24-retell-full-parity-program-roadmap.md`, matrix row *"general_tools"* — currently "accepted/echoed only")

**Migration driver:** `usan-retirement-backend/VOICE_PROVIDER_MIGRATION_SPEC.md` §5 (Surface 3). The client's three agents call **56 tool declarations / 33 unique functions** (`retell/{companion,inbound,sales}/*.json`) — Supabase edge functions invoked mid-call over HTTP with a flat JSON body and an `X-Caller-Secret` header. Our engine cannot do this today: the tool inventory is a **closed set of 15 builtins** and there is no generic HTTP-tool executor. Without Surface 3 the agents connect but can perform none of `log_mood`, `flag_crisis`, `schedule_callback`, `save_conversation_note`, … so this is the long pole of the migration.

---

## 1. Goals & non-goals

### 1.1 Goals
- Accept a **Retell "custom" tool declaration** (`name`, `description`, `url`, `method`, `parameters` JSON-Schema) on `create-retell-llm`/`update-retell-llm`, persist it in the agent version snapshot, and **honor it at call time** (today it is stored-and-echoed only).
- At call time, expose each declared tool to the LLM with its exact JSON-Schema, and on invocation **POST the declared URL with a flat argument body** (`{"phone":"+1…","delay_minutes":15}` — **never** an `args` wrapper), header `X-Caller-Secret: <secret>`, resolving `{{dynamic_var}}` tokens in the URL and string arguments first. Return the tool's 200 JSON to the model.
- Preserve every existing property of the closed builtin catalog (its hard-block validation, its 15 typed `@function_tool` callables, its per-version forward-compat invariant, test-mode no-op registry). External tools are **additive**, never a weakening of the builtin gate.
- Keep PHI-egress discipline: external tool URLs are arbitrary operator-configured HTTPS destinations — exactly the hazard class the outbound-webhooks design (`2026-06-10`) and the Phase 3 tools design already gate. Reuse that discipline, do not re-invent or bypass it.

### 1.2 Non-goals (v1)
- Retell **state-machine tools** (`states` / `starting_state` on the Retell-LLM) — separate parity item (Conversation-Flow, roadmap Phase 6). We parse and persist `general_tools`, not `states`.
- `speak_during_execution` / `speak_after_execution` filler speech and `response_variables` extraction — v1 executes synchronously and returns the raw JSON body; a filler-utterance follow-up is a fast-follow (§9, open Q3).
- Retell **built-in** tool *types* other than a passthrough map for `end_call` (§4.3). `transfer_call`, `check_availability_cal`, `press_digit`, etc. remain out of scope / documented-501-equivalent for tools.
- Per-tool auth schemes beyond the shared `X-Caller-Secret` (OAuth, per-tool bearer). One shared secret per org, matching Retell's single `RETELL_FUNCTION_SECRET`.

---

## 2. The architectural decision: execute in apps/api, not in the agent

Retell's literal model is *the voice engine dials the client URL itself*. We deliberately **do not** mirror that. Instead the agent declares the tool to the LLM but **delegates execution to `apps/api`** via the existing JWT-scoped tool channel (`POST /v1/tools/*`), and `apps/api` makes the outbound HTTPS call.

Why (each reason is load-bearing here):
- **PHI egress + SSRF.** Tool arguments carry PHI (a phone number, a spoken callback note). The allow-list + `ssrf_guard` that gate every off-box POST already live in `apps/api` (`compat/webhook_delivery.py:79`, `SsrfBlocked`, `COMPAT_WEBHOOK_ALLOWED_HOSTS`). Executing there reuses that guard verbatim; executing on the agent VM would require porting SSRF/allow-list into `services/agent`.
- **Secret hygiene.** `X-Caller-Secret` and the client URLs **never leave `apps/api`**. The agent only ever receives the LLM-facing projection of a tool (name/description/parameters) — not its URL, method, headers, or secret. A compromised worker cannot exfiltrate the shared caller secret or the tool endpoints.
- **The no-cross-import boundary (CLAUDE.md).** `apps/api` and `services/agent` do not import each other. Substitution of `{{dynamic_var}}` needs the call's dynamic variables, which are stored server-side (`Call.dynamic_vars`, packed at `compat/call_create.py:121`). Doing substitution + execution server-side keeps the vars where they already live; the agent never needs the raw var map for tools.
- **Org scoping + audit.** The proxy resolves the call → org → published agent version, giving per-org RLS, rate-limit, and PHI-free audit for free — consistent with §2.4/§2.7 of the parity roadmap.

Cost: one extra hop, agent→`apps/api` over the Docker bridge (`http://api:8000`, already the hot path for all 15 builtins) — negligible vs. the client-edge-function round-trip that dominates.

```
LLM decides to call "schedule_callback"(requested_time_text="tomorrow 3pm")
        │  (raw-schema FunctionTool built from the version's tool projection)
        ▼
agent handler  ──POST /v1/tools/external  {call_id, name, arguments}──►  apps/api
        │  (JWT-scoped, call-bound — identical auth to every builtin)          │
        │                                                          resolve call→org→published version
        │                                                          find external tool "schedule_callback"
        │                                                          substitute {{phone}} etc. in url+args
        │                                                          allow-list + ssrf_guard (fail-closed)
        │                                                          httpx POST <client url>  (flat body,
        │                                                             X-Caller-Secret) ──► client edge fn
        │                                                          ◄── 200 JSON
        ◄──────────── {result: <json>} ───────────────────────────────┘
   returns serialized result to the LLM
```

**Rejected alternative — agent dials the client URL directly (Retell-literal).** Closest to Retell's mental model and one hop cheaper, but it pushes SSRF/allow-list/secret-storage/org-context into the worker, egresses the shared caller secret to every agent VM, and breaks the substitution-needs-server-vars story. The one-hop win does not pay for the containment loss.

---

## 3. Data model

### 3.1 `ExternalToolSpec` (server-side, versioned)
Add to `apps/api/.../schemas/agent_config.py` `ToolsConfig`:

```python
class ExternalToolSpec(BaseModel):
    name: str                       # ^[a-zA-Z0-9_-]{1,64}$, unique within a version,
                                    #   and NOT a member of TOOL_NAMES (no builtin shadowing)
    description: str                # 1..1024, shown to the LLM
    url: str                        # https:// only; host must pass allow-list at write AND call time
    method: Literal["POST","GET"] = "POST"
    parameters: dict[str, Any]      # a JSON-Schema object ({"type":"object","properties":{…},"required":[…]})
    timeout_s: float = 10.0         # 1..30
    # v1 has no per-tool secret/header override: the shared org caller-secret is applied server-side.

class ToolsConfig(BaseModel):
    enabled: list[str] = [...]                     # unchanged: the 15 builtins, still TOOL_NAMES-gated
    sms: SmsToolConfig | None = None               # unchanged
    external_tools: list[ExternalToolSpec] = []    # NEW — additive, gated by its own validator
```

The agent-side mirror (`services/agent/.../agent_config.py`) gets a **leaner** parallel copy carrying only the LLM-facing projection — `name`, `description`, `parameters` (no `url`/`method`/`timeout_s`/secret). Extra server fields are dropped by the runtime endpoint (§5), not merely ignored, so URLs/secrets are never serialized toward the worker.

### 3.2 Why a separate list, not `enabled`
`enabled` is validated against the **closed** `TOOL_NAMES` (`schemas/tool_catalog.py`, hard-block). External tools are an **open** set (arbitrary operator names) and must not enter that gate. Keeping them in a distinct `external_tools` field means:
- the builtin closed-set guard is untouched (no regression risk to the 15 safety-critical tools);
- the per-version snapshot/forward-compat invariant holds — `external_tools` is a normal versioned field, unlike the global `TOOL_CATALOG` constant.

**Name-collision rule (refined during WS-B).** The check is *not* "external name ∉ `TOOL_NAMES`" — a migrated Retell client legitimately names its own edge function `schedule_callback` / `flag_crisis`, which happen to be our Clara builtin names, and it wants *its* function, not ours. The real hazard is **double-registration**: the same LLM-facing name appearing as both an *enabled* builtin **and** an external tool. So:
- a structural `ToolsConfig` validator (`_no_enabled_external_overlap`) rejects a config where a name is in **both** `enabled` and `external_tools` — this is within-config and forward-compat-safe (independent of the growing `TOOL_NAMES` catalog), and it guards the human admin-editor path;
- the **compat ingest resolves the overlap in the client's favor**: an external tool *shadows* a same-named builtin by dropping it from `enabled` (`agent_bridge._apply_tools_overlay`), so the client's `schedule_callback`/`raise_crisis` becomes its edge function with no 422. A normal compat agent ends up with builtins off and tools external.

### 3.3 Snapshot & publish
`external_tools` is part of the `AgentConfig` document that is snapshotted into `AgentProfileVersion` on publish (same path as prompts/voice/tools today). A running call reads the **published** version's tools; editing a draft never affects in-flight calls.

---

## 4. Server: ingest Retell `general_tools`

### 4.1 Parse on create/update-retell-llm
`general_tools` is today `list[Any]` echoed opaquely (`compat/schemas/retell_llm.py:27`). Add a translator (called from the retell-llm create/update service, not the schema, so echo-back is preserved) that walks each entry and classifies by shape:

| Incoming entry | Mapped to |
|---|---|
| `{"type":"custom", "name","description","url","method"?,"parameters"}` (Retell API custom tool) | `ExternalToolSpec` |
| `{"name","description","url","method","parameters"}` with **no `type`** (the client's dashboard-export flat shape, `retell/*.json`) | `ExternalToolSpec` (treat as custom) |
| `{"type":"end_call", …}` | add `"end_call"` to `enabled` (our builtin lifecycle tool) |
| any other `{"type": …}` builtin | ignored in v1 (logged); not fabricated |

The raw `general_tools` list is still stored and echoed verbatim in `LlmResponse` (`extra="allow"`), so a client round-tripping the object sees no field loss — parity §0 "repeat the contract byte-for-byte" holds. The parsed `ExternalToolSpec`s are what the runtime actually executes.

### 4.2 Write-time validation (fail loud, before publish)
- `url` scheme is `https`; host resolves and passes the **same allow-list** used for webhooks (a new `COMPAT_TOOL_ALLOWED_HOSTS`, defaulting to the migration client's Supabase functions host). A disallowed host → `422` at create/update, not a silent runtime failure.
- `parameters` is a JSON-Schema **object** (`type == "object"`, `properties` present). Reject arrays/scalars.
- `name` matches the identifier regex and is unique within the list. It may reuse a *catalog* builtin name (§3.2) — only a clash with an *enabled* builtin is rejected (structurally), and the compat ingest avoids even that by shadowing.
- Duplicate/oversized lists bounded (`MAX_EXTERNAL_TOOLS`, e.g. 40 — the client's busiest agent has 27).

(The `url`-scheme / JSON-Schema-object / name / uniqueness checks are **structural** on `ExternalToolSpec` / `ToolsConfig`; only the config-dependent **host allow-list** is a save-time `external_tool_violations` gate, so tightening the allow-list later never 500s an older snapshot on read.)

### 4.3 The three client gotchas (from backend recon) — handle explicitly
- `kb_lookup` — a **Retell built-in with no backing edge function** (its `url` is the literal placeholder `"RETELL_BUILT_IN — …"`). Detect this placeholder and map to our **native KB retrieval** (bind the agent's `knowledge_base_ids`; the tool becomes a thin wrapper over `/v1/tools/retrieve_kb_context`). Do **not** create an `ExternalToolSpec` with a bogus URL.
- `goodrx_lookup` — points to a **non-existent** function (`/functions/v1/goodrx-lookup`). It will validate (host is allow-listed) but 404 at call time; the executor's error path (§6) returns a calm fallback. Flag for the client to fix or drop before cutover — not our bug.
- `log_outcome` → backed by the client's `end-call` function. It is a *custom* tool from our side (a normal `ExternalToolSpec`); no special handling — the client's URL is authoritative.

---

## 5. Runtime config projection

`GET /v1/runtime/agent-config` already returns the resolved `config` block the worker parses into `AgentConfig`. Extend the serializer so `config.tools.external_tools` carries **only** `{name, description, parameters}` per tool. The worker's leaner `ExternalToolSpec` (§3.1) parses exactly those three; `apps/api`-only fields (`url`, `method`, `timeout_s`) are omitted from the projection. This is the security seam: the worker is structurally incapable of learning a tool's URL or the caller secret.

---

## 6. Agent: build raw-schema tools + delegate execution

New module `services/agent/src/usan_agent/external_tools.py`:

```python
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import RawFunctionTool

def build_external_tools(specs, *, call_id, settings) -> list[RawFunctionTool]:
    tools = []
    for spec in specs:                      # spec: name, description, parameters
        tools.append(_make_tool(spec, call_id=call_id, settings=settings))
    return tools

def _make_tool(spec, *, call_id, settings):
    async def _handler(raw_arguments: dict, context: RunContext) -> str:
        # arguments come straight from the LLM per spec.parameters. No client-side
        # {{var}} resolution, no url — apps/api owns both. We only forward.
        try:
            result = await api_client.call_external_tool(
                call_id, settings, name=spec["name"], arguments=raw_arguments
            )
        except Exception:
            logger.bind(call_id=call_id, tool=spec["name"]).warning("external tool failed")
            return "I had trouble doing that just now, but let's keep going."
        return json.dumps(result)          # 200 JSON body → model
    return function_tool(
        _handler,
        raw_schema={
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    )
```

- `api_client.call_external_tool` mirrors `_post_tool` (`api_client.py:64`) exactly: JWT-scoped, call-bound, `POST /v1/tools/external` with `{"call_id":…, "name":…, "arguments":…}`, 10 s timeout, `raise_for_status`. No new auth path.
- Wiring: in `check_in.build_check_in_agent` / `build_inbound_agent`, the `tools=` list becomes `_select_tools(cfg.tools) + build_external_tools(cfg.tools.external_tools, call_id=call_id, settings=settings)`. Builtins first; an external tool whose name collides with a builtin is already impossible (rejected at write, §4.2) but the agent additionally skips it defensively.
- **Test mode** (`build_test_agent`, `session_kind=="test"`): external tools are represented by **no-op raw tools** that return a canned string and make **no** `/v1/tools/*` call — same discipline as `_TEST_TOOL_REGISTRY`, so a pre-publish Test Audio run exercises the tool surface without egress.
- Error discipline matches every builtin: the handler **never raises** into the session; a failed tool returns a calm spoken fallback so a transient client-edge 500 never crashes a live call to an elderly contact.

## 7. Server: the execution proxy

New handler in `apps/api/.../routers/tools.py`: `POST /v1/tools/external`, auth `require_service_token` + call-bound JWT (identical to the builtin tool endpoints, `routers/tools.py:87`). Request `{call_id, name, arguments}`. Steps:

1. Resolve `call_id` → `Call` → org + the **published** agent version; look up `external_tools[name]`. Unknown name → `404` (tool not on this call's agent).
2. **Substitute `{{var}}`** against `Call.dynamic_vars` in (a) the `url`, (b) every string value in `arguments` (recursively), (c) any static header value. Reuse the token semantics of `prompt_vars.substitute` (single non-recursive pass; unknown token → empty string; a value inserted by substitution is never re-scanned, so a `{{…}}` inside a spoken arg can't inject). An arg the LLM emitted as the literal `{{phone}}` (because the tool description tells it to) becomes the real number here — matching Retell.
3. **Egress guard:** allow-list (`COMPAT_TOOL_ALLOWED_HOSTS`) + `ssrf_guard` on the resolved host (DNS→public-IP gate, fail-closed) — the exact webhook path. Blocked → `502`/logged, handler returns the calm fallback upstream.
4. **Flat body (the migration's load-bearing correctness point).** POST the URL with `json=arguments` **directly** — no `{"args": …}` wrapper. The spec (§5 ⚠️) is explicit: ~30 of the client's 33 functions read the body flat and break on a nested `args`. Central enforcement here means no per-tool config can get this wrong.
5. POST the URL (v1 is POST-only — all 56 client tools are POST, §9 Q4). Header `X-Caller-Secret: <org caller secret>` (+ optional `?caller_secret=` for the reserved query mechanism, off by default). `timeout=spec.timeout_s`, redirects OFF.
6. `2xx` → return `{"result": <parsed JSON, or {"text": <body>} if non-JSON>}`. Non-2xx / timeout → `502` with a PHI-free message; the agent surfaces the fallback.
7. **Audit** (PHI-free): `{org, call_id, tool_name, host, status, latency_ms}` — never args/response bodies.

### 7.1 Where the caller secret lives
A single per-org `tool_caller_secret` (= the client's `RETELL_FUNCTION_SECRET`), stored alongside the org's compat credentials (or a compat setting for the single-tenant migration). Never sent to the worker; read only inside the proxy. `ENFORCE`-style rollout is unnecessary — the secret is always sent; the *client* toggles `ENFORCE_CALLER_AUTH`.

---

## 8. Testing / conformance
- **Unit (agent):** `build_external_tools` yields `RawFunctionTool`s whose `raw_schema` equals the spec; handler forwards `{call_id,name,arguments}` unchanged; handler swallows exceptions into the fallback string; test-mode tools make no HTTP call.
- **Unit (server):** flat-body assertion (**no `args` wrapper**) is the headline test; `{{var}}` substitution in url+args+headers; unknown token→empty; allow-list reject → 422 at write and 502 at call; `X-Caller-Secret` present; GET maps args→query; non-JSON body wrapped as `{"text":…}`; builtin-name collision rejected at write.
- **Integration:** a stub edge-function server receiving a real flat body with the header, `{{phone}}` resolved from a call's `dynamic_vars`; end-to-end from a create-retell-llm carrying a client `general_tools` entry through to the stub receiving the call.
- **Migration fixture:** load the client's actual 56 declarations (`retell/{companion,inbound,sales}/*.json`) through the translator; assert 33 unique `ExternalToolSpec`s (minus `kb_lookup`→KB, minus any built-in `end_call`), and that each validates.

---

## 9. Decisions & remaining questions

**Resolved (review 2026-07-09):**
1. **Caller-secret scope — one per org.** A single `tool_caller_secret` (= the client's `RETELL_FUNCTION_SECRET`), stored with the org's compat credentials, applied to every external tool; never sent to the worker (§7.1). No per-tool override in v1.
2. **Allow-list default — exact client host only.** `COMPAT_TOOL_ALLOWED_HOSTS` is seeded with the single Supabase functions host (`mrnlotdwthdqcaicwyql.supabase.co`), not a `*.supabase.co` wildcard. Tightest SSRF surface for PHI-bearing args; widened deliberately per client.
3. **`speak_during_execution` — synchronous v1.** Tools execute-and-reply synchronously; filler speech is a fast-follow, not v1. Full-fixture scan (all 56 real declarations): **12 companion declarations set `speak_during_execution: true`** (the earlier estimate of 5 undercounted) — those lose their filler-speech UX (a short mid-call pause where Retell spoke) until the follow-up lands. Per-tool `timeout_s` (Q5) bounds the worst case. The `speak_during_execution` boolean is parsed and persisted on `ExternalToolSpec` now (unused in v1) so the fast-follow needs no re-ingest.
4. **GET tools — none; GET support dropped from v1.** Fixture scan: **all 56 declarations are `method:"POST"`**. The `ExternalToolSpec.method` field stays (defaulting `POST`) for forward-compat, but the proxy (§7 step 5) implements POST only; a future GET is a small additive change, not v1 scope.
6. **`end_call` — URL-backed + hang-up (`terminates_call`).** Full-fixture scan corrected the §4.3 assumption: the client's `end_call` is **not** a `{"type":"end_call"}` builtin — it's a `type`-less custom tool with a real `/end-call` URL (disposition logging) AND `end_call_after_speech_with_success: true` (all 3 agents). So `end_call` translates to an external tool carrying `terminates_call` (from `end_call_after_speech_with_success`), projected to the worker; after a **successful** call the handler hangs up via the builtin teardown (`_hang_up`: goodbye → delete_room → shutdown). A failed call does **not** hang up (Retell ends only after success). The `{"type":"end_call"}` builtin→`enabled` path (§4.3) remains for any client that sends that shape.

**Still open (confirm from the migration fixtures, §8):**
5. **Timeout budget** — 10 s default matches builtins; per-tool `timeout_s` (cap 30 s) covers fan-out functions. Confirm none of the client's functions routinely exceed 30 s.
