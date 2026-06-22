# Contracts — RetellAI-Compatible Public API

This directory documents the **external wire contract** the compat surface must honor so a CRM built
on RetellAI can repoint with no code changes. The contract IS the requirement; field names, paths,
status codes, and envelopes are normative.

- **[endpoints.md](./endpoints.md)** — every in-scope endpoint (calls, agents, Retell-LLM, batch,
  voices, concurrency) + the call-event webhook envelope and signature.
- **[admin-compat-keys.md](./admin-compat-keys.md)** — the **native** `/v1/admin` endpoints that
  issue/list/revoke the compat API keys (not part of the RetellAI surface).

> **Terminology**: "**Retell-LLM**" (RetellAI's external resource name) and "**response engine**" (the
> spec's Key-Entities term) are the **same thing** — the prompt/model configuration an agent
> references via `response_engine.llm_id`.

## Conventions (apply to every compat endpoint)

### Base URL & versioning
The compat surface is served at the service root and uses RetellAI's **per-endpoint** version
prefixes (NOT a single global version):

| Prefix | Endpoints |
|--------|-----------|
| *(none)* | `/create-agent`, `/get-agent/{id}`, `/list-agents`, `/update-agent/{id}`, `/delete-agent/{id}`, `/publish-agent-version/{id}`, `/create-retell-llm` (+CRUD + `/list-retell-llms`), `/create-batch-call`, `/list-voices`, `/get-voice/{id}`, `/get-concurrency` |
| `/v2` | `/v2/create-phone-call`, `/v2/get-call/{id}`, `/v2/stop-call/{id}`, `/v2/update-call/{id}` |
| `/v3` | `/v3/list-calls` |

> `create-batch-call` is **unversioned** (`/create-batch-call`, not `/v2/...`). The exact prefix of
> `list-retell-llms` / `publish-agent-version` is pinned against the captured oracle (sources differ).

The native API (`/v1/*`) and `/health` are unchanged and unaffected (FR-004/SC-007).

### Authentication
`Authorization: Bearer <compat_api_key>` on **every** compat endpoint. The key is a single static
token issued per organization (see [admin-compat-keys.md](./admin-compat-keys.md)). Missing / invalid
/ revoked → **401** with the error envelope. The same key value is the HMAC secret the CRM uses to
verify webhook signatures.

### Identifiers
| Id | Format |
|----|--------|
| `call_id` | bare 32-char hex (the native Call UUID hex) |
| `agent_id` | `agent_<32-hex>` |
| `llm_id` | `llm_<32-hex>` |
| `batch_call_id` | `batch_call_<32-hex>` |
| `voice_id` | RetellAI-style string (e.g. `retell-Cimo`), aliased to the curated catalog |

### Timestamps
All timestamps are **Unix epoch milliseconds**; durations are `*_ms`. **One deliberate exception**
(RetellAI-faithful): `create-batch-call` response `scheduled_timestamp` is Unix **seconds** (while its
request `trigger_timestamp` is ms). A batch contract test asserts this unit difference.

### Error envelope
All compat errors use `{"status": <http_status:int>, "message": <string>}` with RetellAI status
codes:

| Code | Meaning |
|------|---------|
| 201 | created (create-phone-call / create-agent / create-retell-llm / create-batch-call) |
| 204 | no content (stop-call / delete-agent / delete-retell-llm) |
| 400 | bad request **and** DNC/quiet-hours explicit block (`message` = `blocked_dnc` / `blocked_quiet_hours`) |
| 401 | missing / invalid / revoked API key |
| 422 | schema validation failure |
| 500 | unhandled error (`message` = `internal error`; never leaks a traceback or PHI) |

### Out-of-scope endpoints (FR-053 / SC-009)
Any RetellAI endpoint not listed in [endpoints.md](./endpoints.md) — conversation-flow, knowledge-base,
chat / chat-agent, web-call, voice add/clone/search, batch-test/test-case/test-run, phone-number
management, MCP/export/playground — returns an explicit, documented **"not supported"** response
(`{"status": 501, "message": "not_supported: <endpoint>"}`), never a silent or misleading success.
These stubs appear in the compat OpenAPI so the unsupported contract is itself documented.

### PHI & webhooks
Full-fidelity webhooks (transcript, recording URL, analysis) are delivered **only** to allow-listed,
attested in-infrastructure destinations (`COMPAT_WEBHOOK_ALLOWED_HOSTS`) — see
[endpoints.md](./endpoints.md#webhooks). PHI-bearing payloads are never delivered to a non-allow-listed
host (FR-022/SC-005).
