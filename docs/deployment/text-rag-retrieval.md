# Text-RAG Retrieval (Phase 5b) — operator note

When a chat agent's bound knowledge bases (`knowledge_base_ids` on its retell-llm) have
ingested chunks (Phase 5), chat replies are augmented with the most relevant chunks via a
pgvector cosine-similarity search. Covers BOTH text channels: `/create-chat-completion` and
the inbound-SMS reply engine (they share `chat_service.generate_agent_reply`). Voice is Phase 5c.

## Served behavior

- No new API operation. `knowledge_base_ids` is bound on create/update-retell-llm (echoed on
  read) and consumed server-side at generation. Retrieval is **invisible** — it only improves
  the answer; there is no new response field.
- Unknown / cross-org `knowledge_base_id` at bind time -> **422** (cross-org is never
  acknowledged; RLS makes it indistinguishable from absent).

## Ships inert

Nothing retrieves until BOTH are set:

- `KB_RETRIEVAL_ENABLED=true`
- `GCP_PROJECT=<project-id>` (the Vertex project with ADC on the VM SA)

With either unset, `generate_agent_reply` behaves exactly as before (no query embed, no spend,
no PHI egress). Requires Phase 5 ingestion to have produced chunks (`KB_EMBEDDING_ENABLED` +
poller), otherwise the search returns nothing and replies are un-augmented.

## Activation order

No migration. To activate, set in Secret Manager `usan-prod-env` AND the VM `infra/.env`
(the tag deploy runs `compose up --env-file infra/.env` and does NOT re-fetch the secret —
update both or the new values silently no-op):

```
KB_RETRIEVAL_ENABLED=true
GCP_PROJECT=usan-retirement
```

Then cut a `v*` tag (or restart the api container).

## Tunables (defaults production-safe, but tune the floor)

- `KB_RETRIEVAL_TOP_K` (default `5`) — max chunks injected.
- `KB_RETRIEVAL_MAX_DISTANCE` (default `0.7`) — cosine-DISTANCE ceiling (0=identical,
  2=opposite); the relevance floor. **Tune against real KB content** — the distance
  distribution is model-specific; too tight injects nothing, too loose injects noise.
- `KB_RETRIEVAL_MAX_CONTEXT_CHARS` (default `8000`) — cap on injected context.
- Reuses `KB_EMBEDDING_MODEL` / `KB_EMBEDDING_LOCATION` for the query embed.

## VERIFY at deploy

The query embed must reach Vertex `text-embedding-005` from `KB_EMBEDDING_LOCATION` and return
768-dim vectors (same check as Phase 5 ingestion — see `docs/deployment/knowledge-bases.md`).
A 403/quota error means the VM SA lacks `roles/aiplatform.user`.

## PHI / security

- Tenant isolation: the search runs as `usan_app` (always RLS-bound); org A can never retrieve
  org B's chunks even if a stale id is passed. `organization_id` is server-set by RLS.
- Logs are counts + bucketed distances only — never chunk text, query text, titles, or ids.
- Best-effort: a query-embed or search failure degrades to a no-context reply, never a 500.

## Known limitations (deferred)

- One extra off-loop Vertex embed round-trip per reply when KBs are bound (acceptable for
  chat/SMS latency).
- The relevance-floor default needs live tuning before relying on retrieval quality.
- No reranking, multi-turn query expansion, or hybrid keyword+vector search (future).
- Voice-RAG (`services/agent`) and the observable `knowledge_base_retrieved_contents_url` are
  out of scope (Phase 5c).
