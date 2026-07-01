# RetellAI Parity Phase 6c — Agent ↔ Conversation-Flow binding fidelity (design)

**Date:** 2026-07-01 · **Type:** Phase design spec · **Status:** Approved — ready for implementation plan

> Phase 6a (flow CRUD, #150) and 6b (flow-component CRUD, #151) let a client create
> conversation flows, but an agent still cannot be *bound* to one: `serialize_agent`
> hard-codes `response_engine={type:"retell-llm"}` and `bind_agent` hard-requires
> `response_engine.llm_id`, so any flow-backed agent is rejected `422`. Phase 6c makes the
> voice `agent_bridge` serialize and accept the `conversation-flow` response-engine variant,
> persisted-not-honored (the flow is echoed but not executed until the 6-runtime phase).

## 1. Goal & scope

Make the voice agent bridge speak the full oracle `response_engine` `oneOf`
(`retell-llm` | `conversation-flow`), so a RetellAI client whose agents are backed by a
conversation flow can create, read, update, and list those agents with zero code changes.

**In scope (voice `agent_bridge` only):**
- `serialize_agent` emits the correct `response_engine` variant from stored state.
- `create-agent` accepts a `conversation-flow` engine (validates the flow in-org, mints a
  fresh voice agent bound to it, publishes).
- `update-agent` accepts a `response_engine` to re-point / switch engine type.
- Conformance freeze for the new variant against the pinned oracle + retell-sdk 5.53.0.

**Non-goals / deferred:**
- **No runtime execution.** The bound flow is stored and echoed; the call still runs the
  default Vertex pipeline. Honoring the DAG is the later **6-runtime** phase. Documented,
  never faked.
- **`custom-llm` engine → `422`** (we never dial an external LLM websocket — PHI
  containment, Constitution II).
- **`chat_agent_bridge` is untouched** — the chat bridge has the same hard-coded
  `retell-llm`; fixing it is a small **6c-chat** follow-up, kept out to hold the diff and
  the leak surface tight (per-phase discipline).
- No migration (rides existing JSONB config). No new endpoints. `KNOWN_GAPS` stays
  `frozenset()`; no 501 promoted (create/get/update/list-agent were already served).

## 2. Oracle ground truth

`ResponseEngine` (oracle §6590) is a `oneOf`:

| Variant | Schema | Required | Optional |
|---|---|---|---|
| `ResponseEngineRetellLm` | §6571 | `type=retell-llm`, `llm_id` | `version` |
| `ResponseEngineConversationFlow` | §6538 | `type=conversation-flow`, `conversation_flow_id` | `version` (nullable) |
| `ResponseEngineCustomLm` | §6557 | `type=custom-llm`, `llm_websocket_url` | — |

- `get-agent` response `response_engine` = `ResponseEngine` (full `oneOf`, §7282).
- `update-agent` response `response_engine` = `RetellResponseEngine` (retell-llm |
  conversation-flow, §7314) — custom-llm excluded from responses anyway.
- `AgentResponse` requires `response_engine` (§7264). Our `AgentResponse` model already
  types it as `dict[str, Any]` with `extra="allow"`, so either variant validates.

## 3. Storage model — no migration

A flow binding is stored as a namespaced **top-level** config key on the agent's profile:

```jsonc
config["compat_response_engine"] = {
    "type": "conversation-flow",
    "conversation_flow_id": "conversation_flow_<hex>",  // canonical re-encoded token
    "version": <int|null>                               // accept-and-echo, not honored
}
```

- Stored **only** for flow-bound agents. Absent ⇒ the unchanged retell-llm self-view
  (`llm_id = encode_llm_id(profile.id)`).
- Sits **outside** the native `AgentConfig` schema, exactly like the proven
  `compat_extras` key. `AgentConfig` is `extra="ignore"`, so `_validate_config`'s
  `AgentConfig.model_validate(config)` ignores the key without stripping it from the dict.
- It is **not** part of `compat_extras['agent']`, so `serialize_agent` never echoes it
  raw — it reads the key explicitly to build the typed `response_engine`.
- `conversation_flow_id` is stored as the **canonical re-encoded token**
  (`ids.encode_conversation_flow_id(decoded_uuid)`), so the echo is stable regardless of
  input formatting.

## 4. Schema change (`compat/schemas/agents.py`)

`ResponseEngine` gains a typed `conversation_flow_id` field (it already carries
`type`/`llm_id`/`version` and is `extra="allow"`):

```python
class ResponseEngine(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "retell-llm"
    llm_id: str | None = None
    conversation_flow_id: str | None = None
    version: int | None = None
```

No other schema changes. `CreateAgentRequest.response_engine` stays required;
`UpdateAgentRequest.response_engine` stays optional.

## 5. Bridge changes (`compat/agent_bridge.py`)

### 5.1 New helpers

- `_provisional_agent_name() -> str` → `f"agent-{uuid4().hex[:8]}"` (a fresh flow agent
  has no prior `create-retell-llm` name).
- `async _validate_flow_id(db, token) -> uuid.UUID` — mirror `_validate_kb_ids`: decode
  (malformed ⇒ `422 "unknown conversation_flow_id"`), then `conversation_flows_repo.get`
  (None ⇒ `422 "unknown conversation_flow_id"`). Cross-org is indistinguishable from
  absent (RLS-scoped repo), so it never acknowledges cross-org existence.
- `_store_flow_engine(config, *, flow_uuid, version)` — sets `config["compat_response_engine"]`
  = `{type, conversation_flow_id: encode(flow_uuid), version}`.
- `_clear_flow_engine(config)` — `config.pop("compat_response_engine", None)` (revert to
  self-llm).

### 5.2 `bind_agent` (create-agent) — branch on `body.response_engine.type`

- **`retell-llm`** (or default `type`): the existing path unchanged (load the profile the
  `llm_id` points at, apply voice + extras, publish).
- **`conversation-flow`**: require `conversation_flow_id` (`422` if missing) →
  `_validate_flow_id` → mint a **fresh** profile
  (`create_profile(name=agent_name or _provisional_agent_name())`) →
  `DEFAULT_AGENT_CONFIG` + voice overlay + `_store_flow_engine` + agent extras →
  `_validate_config` → optional webhook register → `update_draft` → set `channel='voice'` →
  `_publish_and_commit`. Returns `(profile, secret)`.
- **anything else** (`custom-llm`, …): `422 "unsupported response_engine type"`.

### 5.3 `update_agent` — handle `body.response_engine` when present

After the existing voice/extras merge, if `body.response_engine is not None`:
- **`conversation-flow`**: require `conversation_flow_id` → `_validate_flow_id` →
  `_store_flow_engine` (re-points a flow agent, or switches an llm agent to a flow).
- **`retell-llm`**: `llm_id` must equal this agent's own `encode_llm_id(profile.id)`
  (self). Match ⇒ `_clear_flow_engine` (revert to self-llm). A different `llm_id` ⇒
  `409 "cannot bind agent to another agent's llm"` (our one-profile overlay can't
  represent RetellAI's one-llm-many-agents; same limitation as 4c-1's cross-channel
  hijack guard). Missing `llm_id` ⇒ `422`.
- **anything else**: `422 "unsupported response_engine type"`.

The `response_engine` mutation is applied to the same `config` dict that is validated and
persisted via `update_draft`, before `_publish_and_commit` (single publish, single commit).

### 5.4 `serialize_agent` — derive the variant

Extract `_response_engine(profile) -> dict[str, Any]`:

```python
def _response_engine(profile):
    stored = (profile.draft_config or {}).get("compat_response_engine")
    if stored and stored.get("type") == "conversation-flow":
        eng = {"type": "conversation-flow",
               "conversation_flow_id": stored["conversation_flow_id"]}
        if stored.get("version") is not None:
            eng["version"] = stored["version"]
        return eng
    return {"type": "retell-llm", "llm_id": ids.encode_llm_id(profile.id)}
```

`serialize_agent` calls it instead of the hard-coded dict. `serialize_agent_version`,
`get_agent`, `list-agents` (v1), and `get-agent-versions` inherit the fix for free.
`serialize_agent_list_item` (v2) has no `response_engine` field — unchanged.

## 6. Runtime posture (persisted-not-honored)

A conversation-flow agent carries the default/empty prompt config; at call time the engine
runs its normal Vertex pipeline against that config — it does **not** execute the flow DAG.
This is the same persisted-not-honored discipline as phone-number bindings (Phase 2) and
`current_node_id` (Phase 3). Calls are **not blocked** — blocking would diverge from the
program's accept-and-persist posture — the gap is documented, and honoring is the
6-runtime phase. Recorded in `docs/deployment/agent-conversation-flow-binding.md`.

## 7. Tests

- `tests/compat/test_agent_flow_binding.py` — create-agent(conversation-flow) happy path;
  get/list/update echo the flow variant; re-point flow→flow; switch llm→flow and
  flow→self-llm; cross-org flow_id ⇒ 422; missing conversation_flow_id ⇒ 422; malformed
  flow_id ⇒ 422; custom-llm ⇒ 422; foreign llm_id on update ⇒ 409; retell-llm create still
  works unchanged.
- `tests/compat/test_freeze_agents.py` (or the existing agent freeze file) — add a frozen
  conversation-flow `AgentResponse` that `assert_conforms('AgentResponse')` +
  `assert_sdk_roundtrip('retell.types:AgentResponse')`.
- Bridge unit test: `_response_engine` returns retell-llm self-view when the key is absent,
  conversation-flow (with/without version) when present.

## 8. Operator note

`docs/deployment/agent-conversation-flow-binding.md` — records: the conversation-flow
binding is persisted + echoed but **not** executed (runs the default pipeline until
6-runtime); custom-llm is rejected; chat-agent flow binding is a deferred 6c-chat
follow-up; inert until the next `v*` tag (merged ≠ deployed); no new env keys, no
migration.

## 9. Foundational-principle compliance

- **§2.3 Exact paths:** no path drift; the four affected ops (create/get/update/list-agent)
  were already served.
- **§2.4 Auth + RLS:** inherits the compat bearer/RLS plane; the flow existence check is
  RLS-scoped (cross-org ⇒ 422, never acknowledged).
- **§2.6 Error envelope:** 422 malformed/unknown/unsupported, 409 foreign-llm, via
  `CompatError`.
- **§2.2 Capability-bounded:** the flow is echoed, not executed; custom-llm rejected, not
  faked.
