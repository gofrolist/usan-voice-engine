# Knowledge Bases (Phase 5) — operator note

RetellAI-compatible knowledge-base CRUD: 6 ops for creating, querying, and deleting
knowledge bases and their text sources. Backed by an async pgvector ingestion pipeline
that chunks text, embeds via Vertex `text-embedding-005` (768-dim), and stores vectors
in `knowledge_base_chunks` (migration `0047`).

## Served operations

| # | Method | Path | Success |
|---|--------|------|---------|
| 1 | `POST` | `/compat/create-knowledge-base` | 201 |
| 2 | `POST` | `/compat/add-knowledge-base-sources/{knowledge_base_id}` | 201 |
| 3 | `GET` | `/compat/get-knowledge-base/{knowledge_base_id}` | 200 |
| 4 | `GET` | `/compat/list-knowledge-bases` | 200 (bare array) |
| 5 | `DELETE` | `/compat/delete-knowledge-base/{knowledge_base_id}` | 204 |
| 6 | `DELETE` | `/compat/delete-knowledge-base-source/{knowledge_base_id}/source/{source_id}` | 200 |

All ops require a compat key (Bearer token minted in the super-admin UI). Status codes
replicate the oracle exactly: **201 / 201 / 200 / 200 / 204 / 200**.

## Ships inert

No embedding or ingestion runs until ALL of the following are set:

- `KB_EMBEDDING_ENABLED=true`
- `GCP_PROJECT=<project-id>` (existing key; the Vertex project with ADC on the VM SA)
- `KB_INGESTION_POLLER_ENABLED=true`

With any of those unset or false, CRUD ops succeed and KBs persist with `status:
in_progress` indefinitely — no spend, no PHI egress to Vertex. The poller is simply
not started.

## Activation order

Migration `0047` is owner-DDL and runs automatically on the `v*` tag deploy (as the
`usan` owner via `usan-prod-db-owner-url`). The api entrypoint's `usan_app` upgrade
then no-ops — no crash-loop risk.

To activate embedding after deploying:

1. **VERIFY pre-conditions** (see below) before touching flags.
2. Set the following in Secret Manager `usan-prod-env` AND in the VM `infra/.env`
   (the `v*` tag deploy runs `compose up --env-file infra/.env` but does NOT re-fetch
   the secret — both must be updated, or the new values silently no-op in prod):
   ```
   KB_EMBEDDING_ENABLED=true
   GCP_PROJECT=usan-retirement
   KB_INGESTION_POLLER_ENABLED=true
   ```
3. Cut the `v*` tag (or restart the api container) — the lifespan picks up the flag and
   starts the in-process poller. Any KB with `status: in_progress` will be claimed and
   ingested within `KB_INGESTION_POLL_INTERVAL_S` seconds (default 15).

Optional tuning (defaults are production-ready):
- `KB_EMBEDDING_MODEL` (default `text-embedding-005`)
- `KB_EMBEDDING_LOCATION` (default `us-central1` — must be a region, not `global`)
- `KB_INGESTION_POLL_INTERVAL_S` (default `15`)
- `KB_INGESTION_BATCH_SIZE` (default `10`)
- `KB_INGESTION_LEASE_SECONDS` (default `300` — crash-recovery window)

## VERIFY at deploy

Two assumptions must be confirmed before cutting a tag that enables embedding. If either
fails, the `set -e` deploy hard-fails on migration `0047`.

**A. pgvector extension privilege.**
Migration `0047` executes `CREATE EXTENSION IF NOT EXISTS vector` as the `usan` owner.
On Cloud SQL, `pgvector` is on the supported-extensions list and requires no instance
flag — but `CREATE EXTENSION` requires `cloudsqlsuperuser` role membership. Confirm the
`usan` owner has it:
```sql
SELECT rolname FROM pg_roles WHERE rolname = 'usan'
  AND pg_has_role('usan', 'cloudsqlsuperuser', 'member');
```
If not, grant it via Cloud SQL IAM or the Cloud Console before deploying.

**B. Vertex `text-embedding-005` reachability.**
The model must answer from `us-central1` (the default `KB_EMBEDDING_LOCATION`) and must
return 768-dim vectors. Confirm with a one-off call using ADC:
```python
from google import genai
from google.genai import types
client = genai.Client(vertexai=True, project="usan-retirement", location="us-central1")
resp = client.models.embed_content(
    model="text-embedding-005",
    contents=["test"],
    config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT", output_dimensionality=768),
)
assert len(resp.embeddings[0].values) == 768
client.close()
```
A 403 or quota error means the VM service account lacks `roles/aiplatform.user` on the
GCP project; a dim mismatch means the model version changed and the migration column
width would need updating.

## PHI / security

- Source text (`content`) and chunk text are stored in `knowledge_base_chunks` (RLS,
  FORCE, `tenant_isolation` policy) — PHI-adjacent but never echoed in API responses.
- The ingestion poller uses a `SECURITY DEFINER` SQL function (`claim_pending_knowledge_bases`)
  to atomically lease KBs across all orgs without exposing any source text. Processing is
  always re-scoped per-org under RLS (`set_tenant_context` with `is_local=true`).
- Vertex calls use ADC (BAA-covered) — never the Gemini Developer API. Chunk text and
  titles are never logged; only model id and counts are emitted.
- Cross-org access to any KB or source resolves to 404 (RLS), never a leak.

## Posture deviations

- **Text sources only.** `knowledge_base_files` and `knowledge_base_urls` in the request
  → **422**. Files/url-fetch are deferred (SSRF surface + object storage not yet wired).
  Never silently drops submitted files.
- **`content_url`** is an internal reference minted at create time. The content lives in
  the DB; the URL is not publicly served in v1 (conformance requires a string; retrieval
  is deferred to Phase 5b).
- **`enable_auto_refresh`** is persisted and echoed in responses but is a **no-op**
  (url re-fetch is deferred). `refreshing_in_progress` and `last_refreshed_timestamp`
  are never emitted.
- **`knowledge_base_ids`** on retell-llm / agent profiles is **echo-only** this phase —
  the field round-trips for clients but retrieval-augmented generation is not wired (deferred
  to Phase 5b voice-RAG / 5c).
- **Status lifecycle:** `in_progress → complete | error` only. `refreshing_in_progress`
  is never emitted.

## Known limitations (deferred hardening)

These are intentional v1 limitations of the ingestion pipeline (surfaced by adversarial
review). None affect the inert ship or the single-container production topology; they
matter when the poller is enabled and/or before any horizontal scale-out.

- **Transient embed failure marks a KB terminally `error`.** Any exception during
  embedding (including a transient Vertex 429 quota / 503 / network blip) sets
  `status: error`; the poller only re-claims `in_progress` rows, so an errored KB is not
  auto-retried. Large-document failures (the common case) are addressed — embedding is
  now **batched** (≤100 chunks and ≤60 000 chars per Vertex request) so a big source no
  longer overflows the per-request instance/token cap. The remaining gap is the transient
  edge. **Recovery:** POST `add-knowledge-base-sources` with a new source (re-queues the
  KB to `in_progress`), or a manual `UPDATE knowledge_bases SET status='in_progress',
  claimed_at=NULL WHERE id=…` as the owner. A bounded-attempts auto-retry is a follow-up.
- **Multi-replica ingestion is not yet concurrency-safe.** The lease + `SKIP LOCKED`
  claim is correct for crash-recovery, but two pollers (multiple api replicas) could
  re-claim the same KB mid-ingest and race on its chunks. A `UNIQUE(source_id,
  chunk_index)` constraint on `knowledge_base_chunks` makes any such double-insert
  **fail loud** (the second processor errors the KB rather than silently duplicating
  chunks). Before scaling the api beyond one replica, add a mid-processing lease refresh
  (and/or re-lock on `add-sources`). Single-container deployment is unaffected.
- **The embed call holds a pooled DB connection.** Each KB is ingested in one short
  transaction whose connection stays checked out across the (multi-second) Vertex embed
  call. Acceptable for the single-replica, flag-gated deployment; a connection-release
  refactor (load → release → embed → reopen) is a follow-up if the pool is contended.

Migration `0047` (tables `knowledge_bases`, `knowledge_base_sources`, `knowledge_base_chunks`,
`CREATE EXTENSION vector`, HNSW index, SECURITY DEFINER claim function) is applied as the
`usan` owner before `compose up` — same owner-DDL convention as migrations `0036`+ (see
the migrations-need-owner runbook).
