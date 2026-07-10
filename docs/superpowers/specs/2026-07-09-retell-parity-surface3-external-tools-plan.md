# Implementation Plan: Surface 3 — External HTTP Agent Tools

**Date:** 2026-07-09 · **Design:** [`2026-07-09-retell-parity-surface3-external-tools-design.md`](./2026-07-09-retell-parity-surface3-external-tools-design.md) · **Migration driver:** `usan-retirement-backend/VOICE_PROVIDER_MIGRATION_SPEC.md` §5

## Summary

Make the RetellAI `general_tools` declarations **executable** instead of stored-and-echoed. `apps/api` ingests each `custom` tool into a versioned `ExternalToolSpec`, projects an LLM-facing subset to the worker, and — at call time — proxies the LLM's tool call to the client's edge function (flat body, `X-Caller-Secret`, `{{var}}`-substituted, SSRF-guarded). The worker builds LiveKit `raw_schema` tools that delegate execution to `apps/api` over the existing JWT-scoped `/v1/tools/*` channel. Secrets and URLs never leave `apps/api`.

## Technical context

- **No Alembic migration.** `AgentProfileVersion.config` is JSONB (`db/models.py:538`); `external_tools` is a new field in the `AgentConfig` document. The caller secret, allow-list, and feature flag are all **settings**, not columns.
- **No new dependency.** LiveKit `function_tool(raw_schema=…)` / `RawFunctionTool` confirmed in `livekit-agents==1.5.14`; httpx already used for egress.
- **Ships inert.** New flag `COMPAT_EXTERNAL_TOOLS_ENABLED=false` (default off, staged-enable like `WEBHOOK_DELIVERY_ENABLED`, `settings.py:216`). When off: the translator still persists specs, but the runtime projection emits `[]` and the proxy returns 501 — no live behavior change.
- **The no-cross-import boundary holds** (CLAUDE.md): the two `agent_config.py` copies stay parallel; the worker never receives a URL or secret.

## Workstreams (PR-sized, ordered by dependency)

### WS-A — Shared schema: `ExternalToolSpec` (both config copies)
- `apps/api/.../schemas/agent_config.py`: add `ExternalToolSpec` (full: `name, description, url, method, parameters, timeout_s, speak_during_execution`) + `ToolsConfig.external_tools: list[ExternalToolSpec] = []`. Field validators: name regex `^[a-zA-Z0-9_-]{1,64}$`, unique-in-list, **not in `TOOL_NAMES`** (import the closed set), `url` https-only, `parameters` is a JSON-Schema object, `list` bounded by `MAX_EXTERNAL_TOOLS=40`.
- `services/agent/.../agent_config.py`: add the **leaner** `ExternalToolSpec` (`name, description, parameters` only) + the same `external_tools` field. Extra fields ignored (pydantic default) — a server field never breaks the worker.
- **Tests:** validator rejects builtin-name collision, non-object `parameters`, dup names, bad url scheme; agent copy parses a 3-field projection and drops extras.
- **Acceptance:** both copies round-trip a spec; `DEFAULT_AGENT_CONFIG` unchanged (empty list).

### WS-B — Server ingest: `general_tools` translator
- New `apps/api/.../compat/tool_translate.py`: `translate_general_tools(raw: list) -> tuple[list[ExternalToolSpec], list[str]]` (specs + extra `enabled` builtins). Classify per design §4.1: `type:"custom"` and the no-`type` flat shape → `ExternalToolSpec`; `type:"end_call"` → `enabled += ["end_call"]`; `kb_lookup` placeholder URL (`startswith("RETELL_BUILT_IN")`) → **not** a spec, instead ensure the agent's `knowledge_base_ids` binding (design §4.3); other `type` → log-and-skip.
- Call it from `compat/agent_bridge.py` where `create/update-retell-llm` maps into the `AgentProfile` config. Preserve the verbatim `general_tools` echo in `LlmResponse` (unchanged — `extra="allow"`).
- Write-time validation surfaces as **422** (disallowed host, bad schema, collision) — before publish, not at call time.
- **Tests:** the 3 gotchas (`kb_lookup`→KB, `goodrx_lookup` validates but is flagged, `log_outcome`→plain custom); echo-fidelity (raw list survives round-trip); 422 on a non-allow-listed host.
- **Acceptance:** the migration fixture (WS-G) yields 33 unique specs minus `kb_lookup`/built-in `end_call`.

### WS-C — Settings: secret, allow-list, flag
- `apps/api/.../settings.py`: `compat_external_tools_enabled` (`COMPAT_EXTERNAL_TOOLS_ENABLED`, default false); `compat_tool_allowed_hosts` (`COMPAT_TOOL_ALLOWED_HOSTS`, default = the client's single Supabase functions host); `compat_tool_caller_secret` (`COMPAT_TOOL_CALLER_SECRET`, = the client's `RETELL_FUNCTION_SECRET`).
- **v1 = single shared secret via settings** (no column). Note in code: multi-org promotion later = move to a column on the compat org/key record (design §7.1), same read-site.
- **Acceptance:** settings validate at startup; empty allow-list with the flag on is a startup error (fail-closed).

### WS-D — Runtime projection
- `apps/api/.../routers/runtime.py` (`/v1/runtime/agent-config`): when `COMPAT_EXTERNAL_TOOLS_ENABLED`, emit `config.tools.external_tools` as `[{name, description, parameters}]` only — **strip** `url`/`method`/`timeout_s`/`speak_during_execution`. Flag off → emit `[]`.
- **Tests:** projection contains no `url`/secret for any tool; flag off → empty.
- **Acceptance:** the security seam is asserted by a test that greps the serialized payload for any tool URL and fails if present.

### WS-E — Execution proxy `POST /v1/tools/external`
- `apps/api/.../routers/tools.py`: new endpoint, auth `require_service_token` + call-bound JWT (mirror the builtin tool endpoints, `routers/tools.py:87`). Body `{call_id, name, arguments}`.
- Steps (design §7): resolve call→org→**published** version→`external_tools[name]` (404 if absent); substitute `{{var}}` from `Call.dynamic_vars` in url + recursive string args (reuse `prompt_vars.substitute` semantics — port a shared helper or duplicate the single-pass regex); allow-list + `ssrf_guard` (reuse `compat/webhook_delivery` helpers — fail-closed); **POST flat body `json=arguments` (no `args` wrapper)** + `X-Caller-Secret` header, `timeout=spec.timeout_s`, redirects off; 2xx → `{"result": <json | {"text": body}>}`; non-2xx/timeout/blocked → 502; PHI-free audit `{org, call_id, tool_name, host, status, latency_ms}`.
- Flag off → 501.
- **Tests (headline = flat body):** assert the outbound request body has **no `args` key**; `{{phone}}` resolved from a call's dynamic_vars; unknown token→empty; allow-list reject→502; header present; non-JSON→`{"text":…}`; unknown tool→404; flag off→501.
- **Acceptance:** a stub edge server receives a correctly-shaped flat POST with the secret header.

### WS-F — Agent executor + wiring
- New `services/agent/.../external_tools.py`: `build_external_tools(specs, *, call_id, settings) -> list[RawFunctionTool]` per design §6; each handler forwards `{call_id, name, arguments}` and **never raises** (calm fallback string on any error).
- `api_client.py`: `call_external_tool(call_id, settings, *, name, arguments)` — clone of `_post_tool` targeting `/v1/tools/external`.
- `check_in.py`: `build_check_in_agent` / `build_inbound_agent` append `build_external_tools(cfg.tools.external_tools, call_id=call_id, settings=settings)` to `tools=`; builtins first. `build_test_agent` + a `_TEST` path build **no-op raw tools** (canned string, no HTTP) — same discipline as `_TEST_TOOL_REGISTRY`.
- `worker.py`: the mid-call dynamic-vars receiver (`worker.py:549`) rebuilds instructions today — ensure external tools are re-attached on rebuild so a mid-call var update doesn't drop them.
- **Tests:** `raw_schema` equals the spec; handler forwards args unchanged; handler swallows a 500 into the fallback; test-mode tools make no `api_client` call; builtins still present alongside externals.
- **Acceptance:** a unit test drives an LLM tool call through the handler to a mocked `api_client`.

### WS-G — Conformance & migration fixture
- Vendor the client's 56 declarations (`retell/{companion,inbound,sales}/*.json`) as a test fixture; assert the translator produces the expected `ExternalToolSpec` set and each validates.
- End-to-end: `create-retell-llm` with a real client `general_tools` entry → published version → worker projection → LLM tool call → proxy → stub edge server sees the flat body + header + resolved `{{phone}}`.
- Extend the parity conformance harness with a `general_tools`-honored assertion (roadmap §2.1).
- **Acceptance:** green conformance for the external-tools surface; the fixture's 33 unique functions all validate (kb_lookup routed to KB, goodrx flagged).

## Sequencing

```
WS-A ─┬─► WS-B ─┐
      └─► WS-C ─┼─► WS-D ─► WS-F ─► WS-G
                └─► WS-E ─────────────┘
```
WS-A first (shared type). WS-B/C parallel on it. WS-D (projection) and WS-E (proxy) both need A+C; they are independent of each other. WS-F (agent) needs the projection (D). WS-G closes the loop. Natural PR cut: **PR1 = A+B+C (server model + ingest, inert)**, **PR2 = D+E (runtime + proxy)**, **PR3 = F+G (agent + conformance)**. Each squash-merges; merged ≠ deployed until the flag flips.

## Rollout

1. Merge PR1–3 with `COMPAT_EXTERNAL_TOOLS_ENABLED=false` — no runtime change.
2. Set `COMPAT_TOOL_CALLER_SECRET` = client's `RETELL_FUNCTION_SECRET`; seed `COMPAT_TOOL_ALLOWED_HOSTS` = client Supabase host.
3. Flip the flag in a controlled env; place a test call whose agent has one external tool; confirm the stub/real edge function receives the flat body + header and the LLM gets the result.
4. Canary alongside Surface 2A per the migration cutover plan (§9 of the migration spec).

## Risks

- **Flat-body regression** is the highest-severity correctness risk (breaks ~30 client functions silently). Mitigation: the WS-E headline test + central enforcement (no per-tool escape hatch).
- **`{{var}}` substitution parity** — an unresolved token must blank, never leak literal braces to the edge function. Mitigation: reuse `prompt_vars.substitute` semantics verbatim + a token test.
- **Filler-speech gap** — 5 tools set `speak_during_execution:true`; v1 is synchronous, so those add a short pause. Accepted (design §9); the boolean is persisted now so the fast-follow needs no re-ingest.
- **Secret hygiene regression** — a future change that serializes `url`/secret toward the worker. Mitigation: the WS-D grep-the-payload test fails closed.

## Out of scope (tracked, not built here)
Retell `states`/state-machine tools (Conversation-Flow phase); `speak_during_execution` filler + `response_variables`; GET tools (all 56 client tools are POST); per-tool auth beyond the shared secret; multi-org secret column (v1 uses a settings value).
