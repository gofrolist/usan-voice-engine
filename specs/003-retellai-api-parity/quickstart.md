# Quickstart & Validation — RetellAI-Compatible Public API

Runnable scenarios that prove the feature end-to-end. These are the acceptance checks behind the
spec's Success Criteria; the automated versions live in `apps/api/tests/test_compat_*.py`. Details of
each request/response are in [contracts/](./contracts/) and [data-model.md](./data-model.md).

## Prerequisites

```bash
cd apps/api && uv sync
# local stack (Postgres + api) — from repo root:
make up
# the compat surface is additive on the same base URL as the native API, e.g. http://localhost:8000
```

Tests use the existing testcontainers `pg18` + `usan_app` RLS-subject harness:

```bash
cd apps/api
uv run pytest -v -k compat          # the compat suite
ruff check . && ruff format --check . && uv run mypy   # the pre-push gate (CI runs mypy too)
```

## Scenario 1 — Issue a compat key (operator setup)

Super-admin issues a per-org key on the native plane (token shown once):

```bash
curl -X POST "$BASE/v1/admin/compat-keys" -b admin_session.cookie \
  -H 'content-type: application/json' \
  -d '{"organization_id":"<org-uuid>","label":"crm-prod"}'
# → 201 { ..., "api_key": "key_…" }   ← copy this; never shown again
```

Expected: 201 with `api_key`; a second GET `/v1/admin/compat-keys` lists it **without** the secret.

## Scenario 2 — Drop-in outbound call (US1 / SC-001, SC-004)

Point a RetellAI client (or curl) at the base URL with `Authorization: Bearer key_…`:

```bash
curl -X POST "$BASE/v2/create-phone-call" -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"from_number":"+1...","to_number":"+1...","override_agent_id":"agent_<hex>"}'
# → 201, Call object with a 32-char call_id and call_status:"registered"
curl "$BASE/v2/get-call/$CALL_ID" -H "authorization: Bearer $KEY"          # → 200 Call object
curl -X POST "$BASE/v3/list-calls" -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"limit":10}'                     # → {items,has_more,pagination_key}
```

Expected: a real outbound dial; `get-call`/`list-calls` return RetellAI field names with **ms**
timestamps; a brand-new `to_number` auto-creates a Contact (no pre-existing contact needed).

## Scenario 3 — DNC / quiet-hours explicit error (US1 AS-4 / SC-006)

Create a call to a DNC-listed number (or outside `COMPAT_DEFAULT_TIMEZONE` quiet hours):

```bash
# → 400 {"status":400,"message":"blocked_dnc"}   (or "blocked_quiet_hours")
```

Expected: **no** call placed, explicit machine-readable reason — never a silent drop, never an
un-gated dial.

## Scenario 4 — Call-event webhooks (US2 / SC-003, SC-005)

Configure an agent `webhook_url` pointing at an allow-listed (`COMPAT_WEBHOOK_ALLOWED_HOSTS`)
in-infra receiver, place a call, and assert the receiver gets `call_started`, `call_ended`,
`call_analyzed` in `{event, call}` shape. Verify each signature exactly as the CRM does:

```python
from retell import Retell                      # the CRM's own SDK
assert Retell.verify(raw_body, api_key=KEY, signature=headers["x-retell-signature"])
```

Expected: `verify()` returns `True`; `call_ended`/`call_analyzed` carry transcript + analysis; a
webhook destination **not** on the allow-list receives nothing (SC-005).

## Scenario 5 — Agent + Retell-LLM over the API (US3)

```bash
LLM=$(curl -s -X POST "$BASE/create-retell-llm" -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"start_speaker":"agent","general_prompt":"…"}' | jq -r .llm_id)
curl -X POST "$BASE/create-agent" -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d "{\"response_engine\":{\"type\":\"retell-llm\",\"llm_id\":\"$LLM\"},\"voice_id\":\"retell-Cimo\"}"
# → 201 AgentResponse (agent_id, version, is_published, last_modification_timestamp ms)
curl "$BASE/list-agents" -H "authorization: Bearer $KEY"   # shows API-created AND admin-UI agents (one inventory)
```

Expected: round-trips; an unhosted `voice_id` → documented 4xx; RetellAI `model` is ignored (Vertex
pipeline, PHI containment).

## Scenario 6 — Batch call (US4)

```bash
curl -X POST "$BASE/create-batch-call" -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"from_number":"+1...","tasks":[{"to_number":"+1..."},{"to_number":"+1..."}]}'
# → 201 {batch_call_id, total_task_count, scheduled_timestamp, ...}
```

Expected: each task lazy-upserts a Contact and is gated per-target.

## Scenario 7 — Compatibility fidelity (US5 / SC-009)

```bash
curl "$BASE/list-voices" -H "authorization: Bearer $KEY"        # → RetellAI-shaped voices
curl "$BASE/get-concurrency" -H "authorization: Bearer $KEY"    # → concurrency object
curl -X POST "$BASE/create-knowledge-base" -H "authorization: Bearer $KEY"
# → 501 {"status":501,"message":"not_supported: create-knowledge-base"}
```

## Scenario 8 — No native regression + isolation (SC-007 + tenancy)

- `GET $BASE/health` and any `GET $BASE/v1/...` still hit native handlers and return the native
  `{detail}` error shape (compat returns `{status,message}`).
- An org-A key never returns org-B calls/agents/batches (`test_compat_rls_isolation.py`).
- Startup fails fast if any compat path string-equals a native path (`test_compat_mount_isolation.py`).

## Success-criteria coverage

| Scenario | Success criteria |
|----------|------------------|
| 2 | SC-001, SC-004, SC-008 |
| 3 | SC-006 |
| 4 | SC-003, SC-005 |
| 5, 6 | SC-002 (field parity) |
| 7 | SC-009 |
| 8 | SC-007 |
