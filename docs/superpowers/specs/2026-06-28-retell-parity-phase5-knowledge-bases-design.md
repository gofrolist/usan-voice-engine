# RetellAI Parity — Phase 5: Knowledge Bases (management + async pgvector ingestion) — Design

**Date:** 2026-06-28
**Program:** RetellAI full-API-parity (Phase 5 of 7). See `2026-06-24-retell-full-parity-program-roadmap.md`.
**Builds on:** the compat sub-app (`apps/api/src/usan_api/compat/`), the owner-DDL FORCE-RLS migration convention (head `0046`), the in-`apps/api` Vertex/ADC path, and the existing in-process DB-queue poller pattern.
**Status:** approved design → this spec → implementation plan → subagent-driven development → PR (squash-merge on explicit go-ahead). **No `v*` tag.** Ships fully inert.

---

## 1. Goal & scope

Serve RetellAI's **6 knowledge-base CRUD operations** conformantly and back them with a **durable, multi-tenant, async ingestion pipeline** that chunks **text** sources, embeds them on **Vertex** (`text-embedding-005`, 768-dim), and stores the vectors in **pgvector**. A client that calls the RetellAI knowledge-base API against our base URL gets conformant responses and a real, populated vector store.

### In scope (Phase 5)
- All 6 KB ops served (create, add-sources, get, list, delete-kb, delete-source) at the oracle's exact paths/status codes.
- `knowledge_bases` / `knowledge_base_sources` / `knowledge_base_chunks` tables (migration `0047`, owner-DDL, TenantScoped + FORCE-RLS), `CREATE EXTENSION vector`, a `Vector(768)` column + HNSW ANN index.
- A flag-gated, in-process, **cross-org** ingestion poller: `status: in_progress → complete | error`, lease-claimed via a **SECURITY DEFINER** function, processed per-org under RLS.
- A new `embed_texts()` Vertex helper + a chunking utility honoring `min/max_chunk_size`.
- Conformance freeze tests + surface-coverage updates (`KNOWN_GAPS` stays `frozenset()`).

### Out of scope — deferred (named follow-ons)
- **5b — text-RAG retrieval:** honor `knowledge_base_ids` + `kb_config{top_k,filter_score}` on chat agents, retrieve top-k at the chat-completion turn, inject into the prompt, emit `knowledge_base_retrieved_contents`.
- **5c — voice-RAG:** LiveKit-native retrieval inside `services/agent` (a separate cross-service problem; `apps/api` ⊥ `services/agent`).
- **url sources** (server-side fetch → SSRF surface; reuse the webhook `ssrf_guard` when added).
- **file uploads** (`multipart` binary, ≤25 files / ≤50MB → object storage / GCS).
- **auto-refresh** (`enable_auto_refresh` 12h url re-fetch) and the `refreshing_in_progress` status.

`knowledge_base_ids` on the retell-llm/agent stays **echo-only** this phase (the `extra="allow"` capture already round-trips it for clients); the typed binding lands in 5b.

---

## 2. Oracle surface (the correctness contract)

Source of truth: `apps/api/tests/compat/oracle/openapi-final.yaml` (RetellAI v3.0.0). The 6 ops are currently 501-stubbed in `compat/routers/unsupported.py` (lines 38–44).

| # | Method | Path (verbatim) | Content-type | Success | Response |
|---|--------|-----------------|--------------|---------|----------|
| 1 | `POST` | `/create-knowledge-base` | `multipart/form-data` | **201** | `KnowledgeBaseResponse` |
| 2 | `POST` | `/add-knowledge-base-sources/{knowledge_base_id}` | `multipart/form-data` | **201** | `KnowledgeBaseResponse` |
| 3 | `GET` | `/get-knowledge-base/{knowledge_base_id}` | — | **200** | `KnowledgeBaseResponse` |
| 4 | `GET` | `/list-knowledge-bases` | — | **200** | **bare array** of `KnowledgeBaseResponse` (no envelope, no pagination) |
| 5 | `DELETE` | `/delete-knowledge-base/{knowledge_base_id}` | — | **204** (empty) | — |
| 6 | `DELETE` | `/delete-knowledge-base-source/{knowledge_base_id}/source/{source_id}` | — | **200** | `KnowledgeBaseResponse` (full, updated) |

Status codes are inconsistent and must be replicated exactly: **201 / 201 / 200 / 200 / 204 / 200**. Path #6 has the literal singular segment `/source/` between the two path params.

### `KnowledgeBaseResponse` (required: `knowledge_base_id, knowledge_base_name, status`)
No field is `nullable:true` — absence is by **omission** → `response_model_exclude_none=True`.

| Field | Type | Req | Notes |
|-------|------|-----|-------|
| `knowledge_base_id` | str | ✔ | minted `knowledge_base_<hex>` |
| `knowledge_base_name` | str | ✔ | < 40 chars |
| `status` | str enum | ✔ | `in_progress \| complete \| error \| refreshing_in_progress` — we emit only the first three |
| `knowledge_base_sources` | array | — | `oneOf[Document, Text, Url]`; **omitted** while `in_progress`, populated at `complete` |
| `enable_auto_refresh` | bool | — | persisted + echoed; **no-op** (url-only feature) |
| `last_refreshed_timestamp` | int (ms) | — | omitted (text/no-refresh) |
| `max_chunk_size` | int | — | echoed; immutable after create (default 2000) |
| `min_chunk_size` | int | — | echoed; immutable after create (default 400) |

### Source variants (response) — `oneOf` on `type`
Only the **text** variant is produced this phase:
- **Text** (`type:"text"`, required `[type, source_id, title, content_url]`): we emit `title` + a minted `content_url`. The raw text is **never** echoed.
- **Document** (`type:"document"`, `[type, source_id, filename, file_url, file_size]`) and **Url** (`type:"url"`, `[type, source_id, url]`) — deferred.

### Requests
- **`KnowledgeBaseRequest`** (create, required `[knowledge_base_name]`): `knowledge_base_name`, `knowledge_base_texts[{title,text}]`, `knowledge_base_files[binary]`, `knowledge_base_urls[str]`, `enable_auto_refresh`, `max_chunk_size` (600–6000, default 2000), `min_chunk_size` (200–2000, default 400, `< max_chunk_size`).
- **`KnowledgeBaseAddSourcesRequest`** (no required list): only the three source arrays.

---

## 3. Conformance targets

`tests/compat/test_freeze_knowledge_bases.py` (plain `def test_*`):
- `assert_conforms(body, "KnowledgeBaseResponse")` (OAS30 validator).
- `assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")` (pydantic `.model_validate`).
- **list** returns a bare array → validate **each element** against `KnowledgeBaseResponse` (the SDK `KnowledgeBaseListResponse` is a `TypeAlias = List[...]`, not a model).
- a `*_requires_key` (401-without-bearer) test per op.

`KnowledgeBaseResponse` net-required fields for round-trip: `knowledge_base_id, knowledge_base_name, status`. The text source-variant model requires every field (`content_url, source_id, title, type`); `file_size` is `float` in the SDK (irrelevant this phase — no document sources).

---

## 4. Data model — migration `0047` (owner-DDL, FORCE-RLS)

`revision="0047"`, `down_revision="0046"` (single head; bump if another lands first). `upgrade()` first statement is `CREATE EXTENSION IF NOT EXISTS vector` (the `vector` type + `hnsw` access method come from it). Each table: TenantScoped (`organization_id` server-set via `COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())`), `ENABLE` + `FORCE ROW LEVEL SECURITY`, `tenant_isolation` policy (USING + WITH CHECK), `GRANT SELECT,INSERT,UPDATE,DELETE … TO usan_app`, `ix_<t>_organization_id`. FK→`organizations.id` with **no** `ondelete` (TenantScoped NO-ACTION convention); child FKs `ondelete=CASCADE`. Full boilerplate in §12.

### `knowledge_bases`
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid (`gen_random_uuid()`) | → `knowledge_base_<hex>` |
| `organization_id` | uuid | server-set, RLS |
| `name` | text | < 40 chars (validated at schema layer) |
| `status` | text | `in_progress \| complete \| error` (plain text, not a DB enum — matches `chat_type`/`channel` convention) |
| `max_chunk_size` | int | immutable after create |
| `min_chunk_size` | int | immutable after create |
| `enable_auto_refresh` | bool, default false | persisted, no-op |
| `claimed_at` | timestamptz, **nullable** | **internal** poller lease — never serialized (oracle `status` stays `in_progress` while claimed) |
| `error_detail` | text, **nullable** | **internal** — never echoed (not an oracle field) |
| `created_at` / `updated_at` | timestamptz | `now()` |

No unique constraint on `name` (RetellAI KBs are id-keyed; names may duplicate).

### `knowledge_base_sources`
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | → `source_<hex>` |
| `organization_id` | uuid | RLS |
| `knowledge_base_id` | uuid FK→`knowledge_bases.id` `ondelete=CASCADE` | |
| `source_type` | text | `text` this phase (`document`/`url` reserved) |
| `title` | text, nullable | text-source title |
| `content` | text | **raw ingested text — PHI-adjacent, stored, never echoed** |
| `content_url` | text | minted internal-reference URL we echo |
| `created_at` / `updated_at` | timestamptz | |

"Needs ingestion" = a source row with **no chunks yet** (LEFT JOIN `knowledge_base_chunks`). This makes both create (all sources new) and add-sources (only the new sources) converge without a per-source state column.

### `knowledge_base_chunks`
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | |
| `organization_id` | uuid | RLS |
| `knowledge_base_id` | uuid FK `ondelete=CASCADE` | |
| `source_id` | uuid FK→`knowledge_base_sources.id` `ondelete=CASCADE` | |
| `chunk_index` | int | order within the source |
| `content` | text | **chunk text — PHI-adjacent, never echoed** |
| `embedding` | `Vector(768)` (`pgvector`) | not null |
| `created_at` | timestamptz | |
| index | `CREATE INDEX … USING hnsw (embedding vector_cosine_ops)` | cosine; built now to exercise/de-risk the infra (5b queries it) |

### SECURITY DEFINER cross-org claim function (in `0047`)
The single cross-org primitive. Owner-defined (runs on the `v*` owner-DDL deploy), bypasses RLS, atomically lease-claims a batch across **all** orgs, returns only ids (no PHI). `GRANT EXECUTE … TO usan_app`. Explicit `SET search_path` (SECURITY DEFINER hygiene):

```sql
CREATE FUNCTION claim_pending_knowledge_bases(p_limit int, p_lease_seconds int)
RETURNS TABLE(id uuid, organization_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  UPDATE knowledge_bases SET claimed_at = now()
  WHERE id IN (
    SELECT kb.id FROM knowledge_bases kb
    WHERE kb.status = 'in_progress'
      AND (kb.claimed_at IS NULL OR kb.claimed_at < now() - make_interval(secs => p_lease_seconds))
    ORDER BY kb.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  RETURNING knowledge_bases.id, knowledge_bases.organization_id;
$$;
```

The lease (`claimed_at < now() - lease`) makes a crash mid-ingest self-healing: a claimed-but-not-completed KB becomes reclaimable after the lease window. `downgrade()` drops the function + policies + indexes + tables (in reverse); it does **not** drop the `vector` extension (other objects may depend on it later).

---

## 5. Ingestion pipeline (the async worker)

**The poller is the single ingestion code path.** Create/add-sources handlers are thin: validate → persist KB (`status=in_progress`) + source rows (raw text) → **`commit`** → return 201 with `knowledge_base_sources` **omitted**. No inline embedding.

### Architecture (`compat/kb_ingestion.py` + a poller in `compat/kb_ingestion_poller.py`)
Mirrors `retry_orchestrator.py` / `schedule_orchestrator.py` (one-row-per-txn loop). Started as an `asyncio.create_task(run_kb_ingestion_poller(settings, stop))` in the `main.py` lifespan, sharing the existing `stop = asyncio.Event()`; gated by `kb_ingestion_poller_enabled`. Cancellable sleep via `await asyncio.wait_for(stop.wait(), timeout=interval)`. Per-cycle `try/except Exception` logs-and-continues (never fatal). Uses the shared `get_session_factory()` (RLS-subject `usan_app` in prod — **no privileged runtime session**).

### Per cycle
1. **Claim (cross-org):** one txn calls `select(func.claim_pending_knowledge_bases(batch_size, lease_seconds))` → `[(kb_id, org_id), …]`, commit. (SECURITY DEFINER bypasses the default-org connection baseline → sees all orgs.)
2. **Per claimed KB** (its own short txn(s)):
   a. `set_tenant_context(db, org_id)` (is_local=true; transaction-local, auto-reverts on commit → no pool contamination, no cross-org bleed).
   b. Load the KB + its **un-chunked** sources (RLS-scoped to `org_id`).
   c. For each source: chunk `content` honoring `min/max_chunk_size` → **delete any existing chunks for that source** (idempotent re-ingest) → `embed_texts(chunks)` → insert `knowledge_base_chunks` (RLS WITH CHECK + the org-default fills `organization_id`; **never set in app code**).
   d. On success: `status='complete'`, `claimed_at=NULL`. On failure: `status='error'`, `error_detail=<internal>`, `claimed_at=NULL`. **`commit`.**
3. **Never hold a DB connection across the Vertex embed call** — load (txn) → embed (no connection) → write (txn) — so the embed latency can't starve the request pool.

### `embed_texts()` Vertex helper (new, `compat/kb_embeddings.py`)
ADC + Vertex only (never the Gemini Dev API; PHI containment), off-loop via `asyncio.to_thread`. **Regional** client (`text-embedding-005` is regional — `settings.vertex_location` default `"global"` does **not** serve it → a new `kb_embedding_location` default `"us-central1"`). `output_dimensionality=768` pinned to the column width; `task_type="RETRIEVAL_DOCUMENT"`. No-op gate: returns nothing / sets `error` unless `kb_embedding_enabled and settings.gcp_project`. Logs model id + counts only — **never** chunk text. Exact call in §12.

### Status lifecycle
`create`/`add-sources` → `in_progress` → poller → `complete` (or `error`). `add-sources` on a `complete` KB sets it back to `in_progress` (the new sources are un-chunked → re-claimed). `delete-source` does **not** re-trigger (removing a source needs no re-embed). `refreshing_in_progress` is never emitted (no refresh).

---

## 6. Compat surface

New files mirroring the `chats` resource set: `compat/routers/knowledge_bases.py`, `compat/schemas/knowledge_bases.py`, `compat/kb_serializer.py`, `compat/kb_service.py`, `repositories/knowledge_bases.py` (+ sources/chunks repos). Register the router in `compat/app.py` **before** `unsupported.router`. Every mutation in the service calls `await db.commit()` (`get_compat_db` does not autocommit). Repos are flush-only and **never set `organization_id`**.

### Multipart parsing (pinned — the biggest parsing risk)
The retell-sdk encodes `multipart/form-data` with **list/dict fields as a single `json.dumps(...)` blob** (NOT bracket/deepObject, NOT repeated parts — `_base_client._serialize_multipartform`). Wire fields for create:
- `knowledge_base_name` → plain str; `enable_auto_refresh` → `"true"/"false"`; `max_chunk_size`/`min_chunk_size` → numeric str → read via `Form(...)`.
- `knowledge_base_texts` / `knowledge_base_urls` → **one field each holding a JSON string** → `json.loads()` (wrap → 422 on malformed).
- `knowledge_base_files[]` (literal trailing `[]`) → read via `await request.form(); form.getlist("knowledge_base_files[]")` (a plain `File()` param won't match the bracketed name). **Files → 422 this phase** (text-only).

### Validation / errors (`CompatError(code, msg)`)
- name length < 40; `min_chunk_size < max_chunk_size`, each within oracle bounds → 422.
- **`knowledge_base_files` or `knowledge_base_urls` present → 422 fail-loud** ("only text sources supported"). Never silently drop a client's documents.
- create with no sources at all → allowed (empty KB). **VERIFY** (pin in plan): default decision = the poller no-ops an all-sources-chunked (or zero-source) KB straight to `complete`.
- malformed/wrong-prefix `knowledge_base_id`/`source_id` → 422 (decode in `ids.py`).
- 404 when the KB (or source) is absent / other-org (RLS makes cross-org a 404, not a leak).

### `ids.py`
Add `_KB_PREFIX="knowledge_base_"` (matches the oracle response example) + `_KB_SOURCE_PREFIX="source_"` with encode/decode + prefix-validation → `CompatError(422)`.

### `content_url` minting
Required on the text source variant. We mint a **stable internal-reference URL** for each source (format pinned in the plan, e.g. `<configured-public-base>/v1/knowledge-base-source-content/{source_id}`); the content lives in the DB and the URL is **not publicly served in v1** (documented posture, like the web-call `access_token` caveat). Conformance only needs a string.

### Serializer (`kb_serializer.py`)
Pure ORM→RetellAI: `exclude_none`; ids via `ids.encode_*`; `knowledge_base_sources` **omitted** unless `status=='complete'` (then a list of text-source dicts); `enable_auto_refresh` echoed if set; `last_refreshed_timestamp` omitted; `max/min_chunk_size` echoed.

---

## 7. Settings & flags (all inert / default-OFF)
In `settings.py` (mirror the `Field(default=…, alias=…)` convention):
- `kb_embedding_enabled: bool = False` (`KB_EMBEDDING_ENABLED`)
- `kb_embedding_model: str = "text-embedding-005"` (`KB_EMBEDDING_MODEL`)
- `kb_embedding_location: str = "us-central1"` (`KB_EMBEDDING_LOCATION`) — must be a region, not `global`
- `kb_ingestion_poller_enabled: bool = False` (`KB_INGESTION_POLLER_ENABLED`)
- `kb_ingestion_poll_interval_s: int` and `kb_ingestion_batch_size: int` (sane defaults)
- `kb_ingestion_lease_seconds: int` (claim lease; default e.g. 300)

Dimension `768` is baked into `Vector(768)` + `output_dimensionality=768` — changing the model's dimension requires a migration. These keys must be added to **both** the compose `api` `environment:` map **and** the VM `.env` (the deploy doesn't re-fetch the secret) or they silently no-op in prod.

---

## 8. Posture deviations (documented in `docs/deployment/knowledge-bases.md`)
- **text sources only**; `files`/`urls` → 422 (deferred).
- **`content_url`** is an internal reference (content in DB, not publicly fetchable in v1).
- **`enable_auto_refresh`** persisted + echoed but **no-op**; `refreshing_in_progress`/`last_refreshed_timestamp` never emitted.
- **`knowledge_base_ids`** on retell-llm/agent stays **echo-only** (binding + retrieval = 5b/5c).
- **status** lifecycle is `in_progress → complete | error` driven by the poller; ship **inert** (`kb_ingestion_poller_enabled` + `kb_embedding_enabled` + `gcp_project` all required to run).

---

## 9. Security & PHI
- **RLS isolation** on all three tables (FORCE + tenant_isolation). The only cross-org primitive is the **SECURITY DEFINER claim function**, which returns ids only (no PHI), takes no caller-controlled query, sets an explicit `search_path`, and merely flips the lease — processing is always re-scoped per-org under RLS.
- **PHI-safe logging:** `_audit` logs op + `knowledge_base_id` only; ingestion/embedding errors log `type(exc).__name__` + counts/model — **never** source/chunk text, titles, or `error_detail`. (`error_detail` is stored internal-only, never serialized.)
- **No SSRF surface** this phase (no url fetch; text-only). url/file deferral keeps the attack surface minimal.
- Cross-org access through any op resolves to **404** (RLS), never a leak.
- `set_tenant_context` per claimed row is **is_local=true** (txn-local) → no pool-context bleed into request sessions sharing the engine.

---

## 10. Testing strategy
- **Migration:** tables exist, `vector` extension present, RLS enabled+forced, `usan_app` grants, HNSW index present, the SECURITY DEFINER function exists + is EXECUTE-grantable.
- **Repos:** CRUD; **cross-org RLS isolation** (org A can't read/delete org B's KB/source/chunk); chunk insert with a `Vector(768)` round-trips `list[float]`.
- **Serializer:** `exclude_none`; `in_progress` omits `knowledge_base_sources`; `complete` includes the text variant; `assert_conforms` + `assert_sdk_roundtrip`.
- **Service:** validation 422s (name length, chunk bounds, files/urls present, bad id prefix), 404s, commit-on-mutation, status transitions.
- **Multipart:** a test posting the **real retell-sdk wire shape** (JSON-blob `knowledge_base_texts`) parses correctly; malformed JSON → 422; `knowledge_base_files[]` present → 422.
- **Ingestion/poller:** mock `embed_texts`; claim → chunk → embed → `complete`; embed failure → `error`; idempotent re-ingest (delete-then-insert); the **cross-org claim** returns multi-org rows and each is processed under its own org context; lease reclaim of a stale-claimed KB; chunk-size honoring.
- **Router:** all 6 ops, exact status codes (201/201/200/200/204/200), auth-required, freeze conformance.
- **Surface coverage:** remove the 6 KB tuples from `unsupported.py` **and** the `create-knowledge-base` line from `tests/test_compat_fidelity.py`; `KNOWN_GAPS` stays `frozenset()`; both `test_surface_coverage.py` and `test_compat_fidelity.py` stay green.

---

## 11. Rollout / deploy (no `v*` tag this phase)
- Migration `0047` is **owner-DDL** — the `v*` tag deploy runs `alembic upgrade head` as the `usan` **owner** (`usan-prod-db-owner-url`, `build.yml:206-224`) **before** `compose up`, so `CREATE EXTENSION vector` + `FORCE RLS` + `GRANT` + the SECURITY DEFINER function run with owner privileges; the api entrypoint's own `usan_app` upgrade then no-ops (won't crash-loop like 0036).
- **`pgvector` Python dep** baked into the image via `pyproject.toml` / `uv.lock` (`uv add pgvector`, floor `>=0.3.6`).
- New settings keys → compose `environment:` + VM `.env` + Secret Manager `usan-prod-env` (survive reboot), all default-OFF.
- **VERIFY at deploy (ASSUMPTIONS):** (a) the `usan` owner can `CREATE EXTENSION vector` on the prod Cloud SQL instance (needs `cloudsqlsuperuser` membership; pgvector is on Cloud SQL's supported list, no instance flag) — confirm before cutting the tag or the `set -e` deploy hard-fails; (b) `text-embedding-005` answers from `us-central1` and returns 768-dim (`len(values)==768`).

---

## 12. Pinned technical facts (drop-in for the plan)

> Verified against the installed packages: `google-genai` 2.8.0, `retell` (Stainless-gen), `pgvector` to be added. Greenfield — no existing embedding/pgvector code.

### Embedding (Vertex `text-embedding-005`)
```python
from google import genai
from google.genai import types

client = genai.Client(vertexai=True, project=settings.gcp_project,
                      location=settings.kb_embedding_location)  # MUST be a region, e.g. "us-central1"
try:
    resp = client.models.embed_content(
        model=settings.kb_embedding_model,           # "text-embedding-005"
        contents=chunk_texts,                        # list[str], order-preserving
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=768,
        ),
    )
finally:
    client.close()
vectors: list[list[float]] = [e.values for e in (resp.embeddings or [])]
```
`embed_content(*, model, contents, config)` is keyword-only; sync client is blocking → wrap in `asyncio.to_thread`; `close()` in `finally`. `output_dimensionality` ∈ {768(default),512,256,128,64}; pin 768.

### Multipart (retell-sdk wire — DEFINITIVE)
`_base_client._serialize_multipartform`: `isinstance(value,(list,tuple,dict)) → serialized[str_key]=json.dumps(value)`. So `knowledge_base_texts`/`knowledge_base_urls` arrive as **one form field holding a JSON string**; scalars as plain strings; files as parts named `knowledge_base_files[]` (trailing brackets). FastAPI: `Form(...)` for scalars + `json.loads` the two arrays; `await request.form(); form.getlist("knowledge_base_files[]")` for files.

### Poller template
In-process `asyncio.create_task` in the `main.py` lifespan, shared `stop` Event; flag-gated; one-row-per-txn loop; cancellable `asyncio.wait_for(stop.wait(), timeout=interval)`; shared `get_session_factory()`; cross-org via the SECURITY DEFINER claim; `set_tenant_context(db, org_id)` (is_local=true) per row; metrics after commit.

### pgvector / Alembic (migration `0047`, copy from `0046_chat_analyses.py`)
```python
from pgvector.sqlalchemy import Vector
_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"

def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO usan_app")

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")   # FIRST
    # create_table knowledge_bases / knowledge_base_sources / knowledge_base_chunks
    #   org col: server_default=sa.text(_ORG_DEFAULT_EXPR); id: gen_random_uuid()
    #   chunks: sa.Column("embedding", Vector(768), nullable=False)
    #   FK→organizations.id with NO ondelete; child FKs ondelete="CASCADE"
    op.execute("CREATE INDEX ix_knowledge_base_chunks_embedding_hnsw "
               "ON knowledge_base_chunks USING hnsw (embedding vector_cosine_ops)")
    # CREATE FUNCTION claim_pending_knowledge_bases(...) SECURITY DEFINER (see §4)
    for t in ("knowledge_bases", "knowledge_base_sources", "knowledge_base_chunks"):
        _enable_rls(t)
```
ORM: `embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)` on a `Base, TenantScoped` model (don't redeclare `organization_id`). `CREATE EXTENSION` must precede the `vector(768)` column + HNSW index. Owner-DDL deploy-safe (`build.yml` owner-runner). `downgrade()` reverse; do **not** drop the `vector` extension.

---

## 13. Open VERIFY items (carried to the plan / deploy)
1. **Empty-source KB** → `complete` immediately vs `in_progress` (default: `complete`). Pin in plan.
2. **`content_url` format** + the configured public base (pin in plan; documented non-fetchable).
3. **Cloud SQL `CREATE EXTENSION vector` privilege** for the `usan` owner — verify before the tag.
4. **`text-embedding-005` from `us-central1` returns 768-dim** — verify empirically at deploy.
5. **`File(alias="knowledge_base_files[]")`** vs `request.form().getlist()` — prefer the `getlist` fallback (Starlette-version-robust).
