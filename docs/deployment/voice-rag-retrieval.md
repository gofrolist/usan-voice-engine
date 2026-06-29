# Voice-RAG Retrieval (Phase 5c) — operator note

When a voice agent's bound retell-llm config has `knowledge_base_ids`, each caller turn
retrieves the most relevant KB chunks (server-side, RLS-scoped) and injects them into the
system prompt for that turn. The caller hears only the augmented answer — retrieval is
**invisible** and adds no new response field. Phase 5b covers the text channels (chat and
inbound SMS); this note covers the voice channel (`services/agent`).

## How it works

1. The LiveKit worker receives the caller's transcript turn.
2. It calls the API endpoint `POST /v1/tools/retrieve_kb_context` with `{call_id, query}`.
3. The API embeds the query via Vertex `text-embedding-005`, runs a pgvector cosine-similarity
   search scoped to the caller's org (RLS-enforced), and returns a context string.
4. The worker prepends the context to that turn's system prompt before sending it to the LLM.
5. On any error or timeout the worker continues without context — the call is never broken.

## Ships inert

Nothing retrieves until BOTH flags are set to `true` AND `GCP_PROJECT` is configured:

- `KB_RETRIEVAL_VOICE_ENABLED` in **apps/api** (the real embed/search gate; default `False`)
- `KB_RETRIEVAL_VOICE_ENABLED` in **services/agent** (the per-turn-call gate; default `False`)
- `GCP_PROJECT=<project-id>` in apps/api (the Vertex project, with ADC on the VM service
  account)

With any of these missing or `False`, the worker skips retrieval entirely: no query embed, no
Vertex spend, no PHI egress.

Requires Phase 5 ingestion to have produced chunks (`KB_EMBEDDING_ENABLED` + the ingestion
poller running), otherwise retrieval returns nothing and replies are un-augmented.

## Activation order

No migration required. To activate:

**Step 1.** Add or set both keys in Secret Manager `usan-prod-env` AND the VM `infra/.env`.

The tag deploy runs `compose up --env-file infra/.env` and does NOT re-fetch the secret —
update both places or the new values silently no-op (compose-env-passthrough two-place rule).
Each key must also appear in its service's `environment:` block in `docker-compose.yml`; if a
key is absent from the compose map it is never passed into the container even if it is in
`.env`.

```
KB_RETRIEVAL_VOICE_ENABLED=true
GCP_PROJECT=usan-retirement
```

Set `KB_RETRIEVAL_VOICE_ENABLED=true` in BOTH the api service and the agent service
environment maps.

**Step 2.** Cut a `v*` tag (or restart both the api and agent containers).

## Tunables

Voice reuses the same tunable set as chat-RAG — one tuning pass covers both channels:

| Variable | Default | Notes |
|---|---|---|
| `KB_RETRIEVAL_TOP_K` | `5` | Max chunks injected per turn. |
| `KB_RETRIEVAL_MAX_DISTANCE` | `0.7` | Cosine-distance ceiling (0 = identical, 2 = opposite). **Tune against real KB content** — the distance distribution is model-specific; too tight injects nothing, too loose injects noise. |
| `KB_RETRIEVAL_MAX_CONTEXT_CHARS` | `8000` | Cap on total injected context characters. |
| `KB_EMBEDDING_MODEL` | `text-embedding-005` | Vertex embedding model (768-dim). |
| `KB_EMBEDDING_LOCATION` | `us-central1` | Vertex region for embedding calls. |
| `KB_RETRIEVAL_TIMEOUT_S` | `3.0` | Per-turn retrieval timeout (agent-side). On timeout the agent speaks without context — the call continues. |

Set the embed/search tunables (`KB_RETRIEVAL_TOP_K`, `KB_RETRIEVAL_MAX_DISTANCE`,
`KB_RETRIEVAL_MAX_CONTEXT_CHARS`, `KB_EMBEDDING_MODEL`, `KB_EMBEDDING_LOCATION`) in Secret
Manager / `.env` under the **apps/api** service — the api performs the embed and search.
`KB_RETRIEVAL_TIMEOUT_S` is a **services/agent** setting (it bounds the worker's per-turn
retrieval HTTP call); set it under the agent service.

## VERIFY at deploy

1. **Vertex reachability.** The query embed must reach Vertex `text-embedding-005` from
   `KB_EMBEDDING_LOCATION` and return 768-dim vectors. A 403 means the VM service account
   lacks `roles/aiplatform.user`. (Same check as Phase 5 ingestion — see
   `docs/deployment/knowledge-bases.md`.)
2. **Live call with a KB-bound profile.** Place a test call to a profile whose retell-llm
   has `knowledge_base_ids` set. Ask a question answered in the KB. Confirm the answer
   reflects KB content.
3. **Check logs.** Apps/api logs emit counts and bucketed distances per retrieval — never
   chunk text, query text, or identifiers. Confirm no PHI appears in log lines.

## PHI / security

- **Tenant isolation.** The retrieval search runs as `usan_app` (RLS-bound); `organization_id`
  is server-set from the authenticated call context. Org A can never retrieve org B's chunks
  even if a stale `knowledge_base_id` is passed.
- **Worker sends only `{call_id, query}`.** The worker never sends KB ids directly; the API
  derives the org from the call record and applies RLS.
- **Logs are counts-only.** Retrieval log lines record hit count and bucketed distances — never
  query text, chunk text, titles, or KB ids.
- **Best-effort everywhere.** A query-embed or search failure degrades to a no-context reply,
  never a call failure or 500.
- **No new response surface.** Context is injected server-side into the system prompt; it is
  never returned to the caller or exposed via any API field.

## Known limitations

- **Per-turn embed latency.** Each caller turn triggers a Vertex embed round-trip before the
  agent speaks. Latency is bounded by `KB_RETRIEVAL_TIMEOUT_S` (default 3 s); tune this
  value against observed p95 embed latency in your region.
- **Rate-ceiling coupling.** Each per-turn retrieval counts as one tool call against the
  call's shared per-call tool-call rate ceiling (`tool_call_rate`, default 120/minute);
  this is generous for conversational cadence and also bounds a runaway agent's embed spend.
- **Shared context-size cap.** Voice reuses `KB_RETRIEVAL_MAX_CONTEXT_CHARS` (default 8000),
  the same cap as chat. A smaller voice-specific cap (voice answers are shorter) is a possible
  follow-up.
- **Single-org call plane.** The call plane is single-org today; the retrieval endpoint
  inherits the tool plane's single-org RLS posture. Multi-org voice isolation is a future
  phase.
- **No reranking or hybrid search.** Retrieval is pure cosine-similarity (pgvector); no
  keyword+vector hybrid, multi-turn query expansion, or reranking (future).
