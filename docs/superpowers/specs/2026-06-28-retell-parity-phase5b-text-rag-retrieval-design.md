# RetellAI Parity Phase 5b — Text-RAG Retrieval (Design)

**Date:** 2026-06-28
**Program:** RetellAI full-parity (any client repoints base URL with zero changes).
**Builds on:** Phase 5 (KB CRUD + async pgvector ingestion, merged `8661981` / PR #146).
**Status:** design approved 2026-06-28; awaiting spec review → implementation plan.

## 1. Goal

Make Phase 5's ingested vectors actually do something: when a **chat agent** is bound to
one or more knowledge bases (`knowledge_base_ids`), run a pgvector similarity search over
that agent's ingested chunks and inject the top hits into the `apps/api` Vertex chat prompt,
so generated answers reflect the KB content (Retrieval-Augmented Generation).

## 2. Parity framing (what this is and is NOT)

- RetellAI's RAG is **purely internal**. The vendored oracle
  (`apps/api/tests/compat/oracle/openapi-final.yaml`, v3.0.0) exposes **no** client-facing
  KB-query/search/retrieve operation — only the six CRUD ops already shipped in Phase 5.
  `knowledge_base_ids` is bound to the response-engine (RetellLlmRequest /
  ConversationNode / SubagentNode / RetellLlmOverride); retrieval happens server-side,
  invisibly.
- Therefore Phase 5b adds **zero new API operations**. `KNOWN_GAPS` stays `frozenset()`.
- Parity here is **behavioral** (bound KBs influence answers), verified by our own tests —
  **not** a contract-freeze against a captured oracle (there is no observable surface to
  freeze). Existing freeze tests must stay green; no new freeze file for retrieval.
- **Ships inert.** Default-OFF flag, no `v*` tag. With retrieval disabled / no KBs bound /
  no chunks present, `generate_agent_reply` behaves byte-for-byte as it does today.
- **No migration.** `knowledge_base_ids` lives in the existing `agent_profile_versions.config`
  / `agent_profiles.draft_config` JSONB. No DDL, no owner-DDL deploy concern.

## 3. Locked scope decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Channels that honor bound KBs | **Both text channels** — `/create-chat-completion` AND the inbound-SMS reply engine (they share the `generate_agent_reply` chokepoint). Voice deferred to 5c. |
| 2 | Chunk selection | **Top-K + relevance floor** — K nearest, then drop any above a cosine-distance ceiling so an off-topic query injects nothing. |
| 3 | Unknown / cross-org `kb_id` at bind | **Reject at bind time (422)** on create/update-retell-llm. Cross-org is indistinguishable from absent via RLS → never acknowledged. |
| 4 | Observability | **Invisible** — retrieval only improves the answer; no new response field; logs counts/bucketed-distances only. |

## 4. Current-state facts (from recon, file:line)

- **Chunk storage** (`migrations/versions/0047_knowledge_bases.py`, `db/models.py:1438-1462`):
  `knowledge_base_chunks.embedding Vector(768)`, NOT NULL; HNSW index
  `ix_knowledge_base_chunks_embedding_hnsw USING hnsw (embedding vector_cosine_ops)`;
  `UNIQUE(source_id, chunk_index)`; columns incl. `knowledge_base_id`, `source_id`,
  `chunk_index`, `content` (PHI-adjacent, never echoed). TenantScoped, **ENABLE-not-FORCE**
  RLS (see `kb-securitydefiner-force-rls`).
- **Embedding helper** (`compat/kb_embeddings.py`): `embed_texts(texts, settings)` async,
  `task_type="RETRIEVAL_DOCUMENT"`, `output_dimensionality=768`, `auto_truncate=True`,
  `genai.Client(vertexai=True, project=settings.gcp_project, location=settings.kb_embedding_location)`,
  batched (≤100 texts / ≤60 000 chars). `_DIM=768`.
- **KB repo** (`repositories/knowledge_bases.py`): CRUD + `claim_pending` + chunk inserts.
  **No similarity-search function exists.**
- **Generation chokepoint** (`compat/chat_service.py:193-216` `generate_agent_reply`):
  `_load_published_config` → `AgentConfig.model_validate(version.config)` → reads
  `cfg.prompts.system_prompt`, `cfg.llm.model`, `cfg.llm.temperature` → builds
  `system_instruction` + role-mapped `contents` from full history → `run_vertex_turn(...)`.
  Serves **both** `/create-chat-completion` (`routers/chats.py`) and the inbound-SMS reply
  engine (`compat/sms_reply.py` → `webhooks.py:143`).
- **Binding today** (`compat/agent_bridge.py`, `compat/chat_agent_bridge.py`): a retell-llm
  is an `AgentProfile` (the "response-engine half"); its fields land in
  `draft_config["compat_extras"]["llm"]` via `_merge_extras` (`extra="allow"`), echoed on
  GET/LIST by `serialize_llm`. A chat-agent binds onto that profile (`channel='chat'`).
  `knowledge_base_ids` sent today is **accepted + echoed but never read** at generation.
  `AgentConfig` is `frozen` + `extra="ignore"`, re-validated on every read — forward-compat
  invariant at `schemas/agent_config.py:517-522`: any new field MUST be Optional w/ default.
- **Settings** (`settings.py:248-262`): `kb_embedding_model` (`text-embedding-005`),
  `kb_embedding_location` (`us-central1`), plus Phase 5 ingestion flags.

## 5. Binding the KBs (Approach A — typed `LLMConfig` field)

**Chosen — A:** promote `knowledge_base_ids` to a first-class typed field.

- Add to `LLMConfig` (`schemas/agent_config.py`):
  `knowledge_base_ids: list[str] | None = Field(default=None)` — Optional+default satisfies
  the frozen/re-validate forward-compat invariant (old published configs without the field
  still validate).
- `_apply_llm_overlay` (`compat/agent_bridge.py`) writes the **encoded public `kb_…` ids**
  into native `config["llm"]["knowledge_base_ids"]` from the retell-llm request — so
  generation reads `cfg.llm.knowledge_base_ids`.
- The existing `_merge_extras(..., "llm", ...)` echo into `compat_extras["llm"]` is unchanged,
  so GET/LIST retell-llm responses stay byte-identical (the SDK round-trip is unaffected).
- Stored representation = encoded `kb_…` strings (the API representation); decoded to UUIDs
  at retrieval. Keeps version snapshots human-meaningful and avoids raw UUIDs in config.

**Rejected — B:** read `knowledge_base_ids` straight from the untyped `compat_extras` blob at
generation. Fragile; mixes the echo blob with execution semantics; no type safety.

**Rejected — C:** a separate KB-binding join table. Overkill; RetellAI models the binding as a
config array on the response-engine. YAGNI; would also reintroduce a migration.

**Bind-time validation (decision #3):** in `create_response_engine` / `update_response_engine`
(create/update-retell-llm), for each submitted `knowledge_base_id`: `ids.decode_kb_id` then
`kb_repo.get_kb(db, uuid)` under RLS. Any id that fails to decode, or resolves to `None`
(absent **or** cross-org — RLS makes these indistinguishable), → **422** with a generic
message (`"unknown knowledge_base_id"`), never echoing which id or acknowledging cross-org
existence. Empty/None list is valid (no binding). Validation happens before persist.

## 6. Retrieval pipeline (three small, isolated units)

### 6.1 Query embedding — `compat/kb_embeddings.py`
`async def embed_query(text: str, settings: Settings) -> list[float]` — one 768-dim vector,
`task_type="RETRIEVAL_QUERY"` (asymmetric retrieval vs ingestion's `RETRIEVAL_DOCUMENT`),
`output_dimensionality=768`, `auto_truncate=True`. Reuses the existing
`genai.Client(vertexai=True, …)` construction and the off-loop `asyncio.to_thread` pattern.
Shape-guards the result (len == 768) and raises on mismatch (caught upstream → degrade).

### 6.2 Vector search — `repositories/knowledge_bases.py`
```python
@dataclass(frozen=True)
class ChunkHit:
    knowledge_base_id: uuid.UUID
    content: str
    distance: float

async def search_chunks(
    db, *, kb_ids: list[uuid.UUID], query_embedding: list[float],
    limit: int, max_distance: float,
) -> list[ChunkHit]:
    ...
```
SQL (RLS-scoped, parameterized; pgvector `<=>` cosine distance matching the HNSW
`vector_cosine_ops` index):
```sql
SELECT knowledge_base_id, content, (embedding <=> :q) AS distance
FROM knowledge_base_chunks
WHERE knowledge_base_id = ANY(:kb_ids)
ORDER BY embedding <=> :q
LIMIT :limit
```
Then drop hits with `distance > max_distance` (the relevance floor). Empty `kb_ids` → `[]`
(no query issued). **RLS isolation:** `usan_app` is always policy-bound, so org A cannot
retrieve org B's chunks even if a stale/foreign `kb_id` is passed — the row simply isn't
visible. The query embedding is bound as a pgvector parameter (no string interpolation).

### 6.3 Orchestration — `compat/kb_retrieval.py` (new)
```python
@dataclass(frozen=True)
class RetrievedContext:
    text: str        # "" when nothing retrieved
    hit_count: int

async def retrieve_context(
    db, settings, *, kb_ids: list[str], query: str,
) -> RetrievedContext:
    ...
```
Steps: gate (`not settings.kb_retrieval_enabled` or `not settings.gcp_project` or not
`kb_ids` or blank `query` → `RetrievedContext("", 0)`); decode `kb_ids` → UUIDs (skip
undecodable defensively); `embed_query`; `search_chunks(top_k, max_distance)`; assemble a
delimited block, appending chunk contents until `kb_retrieval_max_context_chars` is reached
(never split mid-chunk past the cap — stop before exceeding). Returns the block + hit count.
PHI-safe: logs only `hit_count`, candidate count, and bucketed distances — never chunk text,
query text, titles, or ids.

## 7. Injection at the chokepoint — `compat/chat_service.py`

In `generate_agent_reply` (serves both text channels), after `_load_published_config`:

1. `kb_ids = cfg.llm.knowledge_base_ids or []`.
2. If `kb_ids` and `settings.kb_retrieval_enabled`: pick the **query** = the latest `user`/`sms`
   message in the loaded history (the turn being answered); if none, skip.
3. `retrieved = await retrieve_context(db, settings, kb_ids=kb_ids, query=query_text)`,
   wrapped in `try/except` — on any exception log `type(exc).__name__` + counts and continue
   with `RetrievedContext("", 0)`. **Retrieval never breaks a reply.**
4. If `retrieved.text`: append to `system_instruction`:
   `"\n\nKnowledge base context:\n{retrieved.text}\n\nUse the above context to answer when relevant."`
5. Proceed to `run_vertex_turn` unchanged otherwise.

Adds one off-loop Vertex embed round-trip per reply **only** when KBs are bound and the flag
is on. Acceptable for chat (already multi-second); same for SMS.

## 8. Settings (ship-inert) — `settings.py`

```python
kb_retrieval_enabled: bool = Field(default=False, alias="KB_RETRIEVAL_ENABLED")
kb_retrieval_top_k: int = Field(default=5, alias="KB_RETRIEVAL_TOP_K")
kb_retrieval_max_distance: float = Field(default=0.7, alias="KB_RETRIEVAL_MAX_DISTANCE")
kb_retrieval_max_context_chars: int = Field(default=8000, alias="KB_RETRIEVAL_MAX_CONTEXT_CHARS")
```
Retrieval requires `kb_retrieval_enabled AND gcp_project`. `kb_retrieval_max_distance` is a
cosine-distance ceiling (0 = identical, 2 = opposite); **0.7 is a permissive starting
default documented as tune-against-real-data** — the distance distribution is
model-specific and must be validated against real KB content before relying on it. Reuses
`kb_embedding_model` / `kb_embedding_location` for the query embed.

## 9. Error handling / PHI / security

- **Best-effort retrieval:** every embed/search step is wrapped; failure → no-context reply,
  never a 500. Mirrors the never-raise discipline of `summarization.py` / `chat_analysis.py`,
  adapted (the caller still returns a reply).
- **PHI-safe logging:** counts + bucketed distances only. Never chunk text, query text,
  source titles, or kb ids. `organization_id` is server-set by RLS, never by app code.
- **Tenant isolation:** `usan_app` is always RLS-bound; cross-org chunks are invisible.
  Bind-time validation never reveals cross-org existence (generic 422).
- **No SQL injection:** the embedding is a bound pgvector parameter; `kb_ids` is a bound
  UUID array.
- `apps/api` and `services/agent` stay decoupled (voice-RAG is 5c, separate).

## 10. Testing matrix

Behavioral (no new contract-freeze surface):

- **`embed_query`**: uses `task_type="RETRIEVAL_QUERY"`, returns 768-dim; shape mismatch raises.
- **`search_chunks`**: returns hits ordered by ascending distance; respects `limit`; drops
  hits above `max_distance`; empty `kb_ids` → `[]`; **cross-org RLS isolation** — an org-A
  search with org-B's `kb_id` returns nothing (two-orgs fixture).
- **`retrieve_context`**: gating (flag off → empty; no kb_ids → empty; no gcp_project →
  empty; blank query → empty); char-cap honored; PHI-safe (assert no chunk/query text in logs).
- **`generate_agent_reply`**: injects the block when KBs bound + flag on + a match exists;
  `system_instruction` unchanged on no-match; retrieval exception → reply still produced
  (graceful degrade); both channels exercise the same path (api_chat + sms).
- **`LLMConfig` forward-compat**: a published config JSON without `knowledge_base_ids` still
  validates; with it, round-trips.
- **retell-llm bind validation**: create/update-retell-llm with an unknown `kb_id` → 422;
  with a cross-org `kb_id` → 422; with valid ids → persisted in native `config["llm"]` AND
  echoed via `compat_extras`.
- **Freeze stays green**: `test_freeze_agents` / `test_freeze_chat_agents` (and the retell-llm
  response path) still pass `assert_conforms` + `assert_sdk_roundtrip` with
  `knowledge_base_ids` present.
- Query-embed is mocked in tests (reuse the Phase 5 `mock_embed` fixture pattern); no live
  Vertex calls in the suite.

## 11. Out of scope (deferred)

- Voice-RAG in `services/agent` (Phase 5c).
- The observable `knowledge_base_retrieved_contents_url` (a call/voice-response concern).
- `enable_auto_refresh` URL re-fetch (still a no-op from Phase 5).
- Reranking, multi-turn query expansion, hybrid keyword+vector search.
- Binding `knowledge_base_ids` on voice agents / ConversationNode / SubagentNode (chat only).

## 12. Files touched (anticipated)

- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (LLMConfig field)
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (overlay write + bind 422 validation)
- Modify: `apps/api/src/usan_api/compat/kb_embeddings.py` (`embed_query`)
- Modify: `apps/api/src/usan_api/repositories/knowledge_bases.py` (`search_chunks` + `ChunkHit`)
- Create: `apps/api/src/usan_api/compat/kb_retrieval.py` (`retrieve_context` + `RetrievedContext`)
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (injection in `generate_agent_reply`)
- Modify: `apps/api/src/usan_api/settings.py` (4 ship-inert fields)
- Create: `docs/deployment/text-rag-retrieval.md` (operator note: flags, tuning, ship-inert,
  VERIFY-at-deploy for query-embed reachability)
- Tests: `tests/compat/test_kb_retrieval.py`, `test_kb_embeddings.py` (extend),
  `tests/test_knowledge_bases_repo.py` (extend — search + cross-org),
  `tests/compat/test_chat_service*.py` (injection), `tests/compat/test_retell_llm*.py`
  (bind 422), plus freeze-still-green coverage.

No new migration. `KNOWN_GAPS` unchanged (`frozenset()`).
