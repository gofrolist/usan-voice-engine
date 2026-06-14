# Contract: Agent test-session dispatch metadata

**Feature**: `001-retellai-parity-admin` | **Date**: 2026-06-13

Defines how the API hands a **draft** profile config to the agent for a sandboxed Test Audio session
over the existing LiveKit explicit-dispatch channel, without crossing the api↔agent import boundary
(Constitution I) and without producing any production record or PHI egress (Constitution II).

## Dispatch metadata (`CreateAgentDispatchRequest.metadata`)

The API's `dispatch_test_agent(...)` extends the existing `CallMetadata` JSON with:

| Field | Type | Meaning |
|-------|------|---------|
| `session_kind` | `"call" \| "test"` (default `"call"`) | `"test"` selects the sandbox branch. Existing calls omit it → `"call"`. |
| `test_config` | `object \| null` | The full draft `AgentConfig` document (validated by the agent via `AgentConfig.model_validate`). Present only for `session_kind=="test"`. |
| `dynamic_vars` / `resolved_vars` | `object` | Admin-supplied **synthetic** sample values only. No real contact lookup occurs in test mode. |
| `direction` | `"inbound" \| "outbound"` | Which flow to simulate. |

## Agent behavior when `session_kind == "test"`

`parse_metadata` populates the new fields; `entrypoint()` branches:

1. Build `AgentConfig` from `test_config` (do **not** call the published-only config resolver / inbound lookup).
2. Register the **no-op `_TEST_TOOL_REGISTRY`** — stub `@function_tool` callables that return canned strings and **never** call `api_client`/`/v1/tools/*`. This is the *only* tool registry reachable in test mode.
3. **Skip** all side effects: no inbound-lookup, no `register_transcript_flush`, no `register_metrics_flush`, no `start_call_recording`/egress, no SIP/Telnyx participant.
4. Wait for a participant **generically** (browser WebRTC join) — read no `sip.*` attributes.
5. Honor the existing `max_call_duration_s` watchdog as the bound on test length.

## Invariants (verified by tests)

- A test session writes **no** `Call`, `WellnessLog`, `MedicationLog`, or audit row (FR-027 / SC-009).
- No real contact PHI is loaded — only admin-supplied `sample_vars` substitute into prompts (FR-026).
- No phone number is consumed and no PSTN call is placed — the browser joins the throwaway room directly (FR-028).
- The agent's LLM/TTS/STT are the live Vertex/Cartesia plugins (faithful), so the LLM path stays on Vertex AI via ADC (Constitution II).
- `session_kind` defaults to `"call"`, so every existing outbound/inbound dispatch is byte-compatible (no regression).
