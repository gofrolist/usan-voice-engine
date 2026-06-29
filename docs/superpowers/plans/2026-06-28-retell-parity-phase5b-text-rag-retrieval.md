# RetellAI Parity Phase 5b — Text-RAG Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a chat agent is bound to one or more knowledge bases, run a pgvector similarity search over its ingested chunks and inject the top hits into the `apps/api` Vertex chat prompt so generated answers reflect the KB content.

**Architecture:** Promote `knowledge_base_ids` to a typed `LLMConfig` field (validated 422 at create/update-retell-llm), add a query-embedding helper + an RLS-scoped vector-search repo function + a small orchestration module, then inject retrieved context at the single shared chokepoint `chat_service.generate_agent_reply` (serves both `/create-chat-completion` and the inbound-SMS reply engine). Ships inert behind `kb_retrieval_enabled`; migration-free (rides existing JSONB config).

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy async + pgvector (`Vector(768)`, `cosine_distance`/`<=>`, HNSW `vector_cosine_ops`) / Vertex `text-embedding-005` via `google-genai` (`vertexai=True`, ADC) / Pydantic settings / loguru / pytest (`-n auto`).

## Global Constraints

- PHI/secret-safe logging only: never log chunk text, query text, source titles, or kb ids — counts + bucketed distances only.
- `organization_id` is server-set by RLS, never by app code.
- `apps/api` and `services/agent` must NEVER import each other (voice-RAG is Phase 5c, out of scope).
- `exclude_none` discipline on every serialized response.
- CI mypy = `uv run mypy` with config `files=["src"]` — NEVER `mypy .`.
- ruff: line-length 100, target py314 (api). Run `ruff check . && ruff format .` before every commit.
- This env's text display strips parens from `except (A, B):` — verify syntax via `python -m py_compile`, not by eye.
- `AgentConfig` is `frozen` + `extra="ignore"`, re-validated on every read — any NEW field MUST be Optional with a default (forward-compat invariant at `apps/api/src/usan_api/schemas/agent_config.py:517-522`).
- pytest runs parallel by default (`-n auto`); any cross-org / global-state test must tolerate sibling rows. `_TRUNCATE_ALL` (conftest) already covers the 3 KB tables; the `two_orgs` fixture seeds/cleans its own orgs.
- Ships INERT: `kb_retrieval_enabled` default `False`; no `v*` tag. `KNOWN_GAPS` stays `frozenset()`. No migration.
- The KB public-id prefix is `knowledge_base_` (e.g. `knowledge_base_<hex>`); decode via `ids.decode_kb_id` (raises `CompatError(422)` on malformed).
- All commands run from `apps/api`. Tests: `uv run pytest <path> -v` (add `-n0` for `-s`/pdb).

---

### Task 1: Retrieval settings (ship-inert)

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (after the Phase 5 KB ingestion block, currently ending at line 262)
- Test: `apps/api/tests/test_settings.py` (extend; if absent, create)

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.kb_retrieval_enabled: bool`, `Settings.kb_retrieval_top_k: int`, `Settings.kb_retrieval_max_distance: float`, `Settings.kb_retrieval_max_context_chars: int`.

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/test_settings.py` (mirror how other settings defaults are asserted in that file; if the file does not exist, create it with the import used elsewhere, e.g. `from usan_api.settings import Settings`):

```python
def test_kb_retrieval_defaults_ship_inert() -> None:
    s = Settings()
    assert s.kb_retrieval_enabled is False
    assert s.kb_retrieval_top_k == 5
    assert s.kb_retrieval_max_distance == 0.7
    assert s.kb_retrieval_max_context_chars == 8000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings.py::test_kb_retrieval_defaults_ship_inert -v`
Expected: FAIL with `AttributeError` (fields not defined).

- [ ] **Step 3: Add the fields**

In `apps/api/src/usan_api/settings.py`, immediately after the `kb_ingestion_max_attempts` line (line 262) and before the `# --- Clara Care Parity` comment (line 264):

```python

    # Knowledge-base text-RAG retrieval (Phase 5b). Ship-inert: default OFF, so no query
    # embed (spend or PHI egress) until a deploy enables it AND gcp_project is set. Reuses
    # kb_embedding_model / kb_embedding_location for the query embed. max_distance is a cosine
    # DISTANCE ceiling (0=identical, 2=opposite) — the relevance floor; 0.7 is a permissive
    # starting default that MUST be tuned against real KB content (model-specific distribution).
    kb_retrieval_enabled: bool = Field(default=False, alias="KB_RETRIEVAL_ENABLED")
    kb_retrieval_top_k: int = Field(default=5, alias="KB_RETRIEVAL_TOP_K")
    kb_retrieval_max_distance: float = Field(default=0.7, alias="KB_RETRIEVAL_MAX_DISTANCE")
    kb_retrieval_max_context_chars: int = Field(
        default=8000, alias="KB_RETRIEVAL_MAX_CONTEXT_CHARS"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings.py::test_kb_retrieval_defaults_ship_inert -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/settings.py tests/test_settings.py
git commit -m "feat(api): Phase 5b — add ship-inert KB retrieval settings"
```

---

### Task 2: `knowledge_base_ids` on `LLMConfig` (forward-compat)

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py:148-150` (the `LLMConfig` class)
- Test: `apps/api/tests/test_agent_config.py` (extend; if absent, create)

**Interfaces:**
- Consumes: nothing.
- Produces: `LLMConfig.knowledge_base_ids: list[str] | None` (default `None`), read at generation as `cfg.llm.knowledge_base_ids`.

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/test_agent_config.py` (import: `from usan_api.schemas.agent_config import AgentConfig, LLMConfig`):

```python
def test_llm_config_knowledge_base_ids_defaults_none() -> None:
    assert LLMConfig().knowledge_base_ids is None


def test_agent_config_forward_compat_without_kb_ids() -> None:
    # An OLD published config snapshot has no knowledge_base_ids under llm — must still validate.
    cfg = AgentConfig.model_validate(
        {"prompts": {"system_prompt": "hi"}, "llm": {"model": "gemini-3.1-flash-lite"}}
    )
    assert cfg.llm.knowledge_base_ids is None


def test_agent_config_round_trips_kb_ids() -> None:
    cfg = AgentConfig.model_validate(
        {
            "prompts": {"system_prompt": "hi"},
            "llm": {"model": "gemini-3.1-flash-lite", "knowledge_base_ids": ["knowledge_base_x"]},
        }
    )
    assert cfg.llm.knowledge_base_ids == ["knowledge_base_x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_config.py -k knowledge_base_ids -v`
Expected: FAIL (`knowledge_base_ids` not a field; the round-trip drops it via `extra="ignore"`).

- [ ] **Step 3: Add the field**

In `apps/api/src/usan_api/schemas/agent_config.py`, change `LLMConfig` (lines 148-150) to:

```python
class LLMConfig(BaseModel):
    model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # Phase 5b: KB ids bound to this response engine for text-RAG. Encoded public ids
    # (knowledge_base_<hex>). Optional+default to satisfy the frozen/re-validate forward-compat
    # invariant. Echoed via compat_extras; consumed at chat generation.
    knowledge_base_ids: list[str] | None = Field(default=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_config.py -k knowledge_base_ids -v`
Expected: PASS (all three).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/schemas/agent_config.py tests/test_agent_config.py
git commit -m "feat(api): Phase 5b — add knowledge_base_ids to LLMConfig (forward-compat)"
```

---

### Task 3: Query embedding (`embed_query`)

**Files:**
- Modify: `apps/api/src/usan_api/compat/kb_embeddings.py`
- Test: `apps/api/tests/compat/test_kb_embeddings.py` (extend)

**Interfaces:**
- Consumes: `Settings.gcp_project`, `Settings.kb_embedding_model`, `Settings.kb_embedding_location`.
- Produces: `async def embed_query(text: str, settings: Settings) -> list[float]` (one 768-dim vector); internal `def _embed_query_sync(text: str, settings: Settings) -> list[float]`.

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/compat/test_kb_embeddings.py` (mirror the existing `test_embed_sync_sets_auto_truncate`; reuse its `_settings()` helper and `kb_embeddings` import):

```python
def test_embed_query_sync_uses_retrieval_query_task_type(monkeypatch) -> None:
    """Query embedding MUST use the asymmetric RETRIEVAL_QUERY task type (vs ingestion's
    RETRIEVAL_DOCUMENT) and request 768 dims."""
    captured: dict = {}

    class _FakeEmbedding:
        values = [0.0] * 768

    class _FakeResp:
        embeddings = [_FakeEmbedding()]

    class _FakeModels:
        def embed_content(self, *, model, contents, config):
            captured["config"] = config
            captured["contents"] = contents
            return _FakeResp()

    class _FakeClient:
        models = _FakeModels()

        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(kb_embeddings.genai, "Client", _FakeClient)
    out = kb_embeddings._embed_query_sync("how do I reset my password?", _settings())
    assert captured["config"].task_type == "RETRIEVAL_QUERY"
    assert captured["config"].output_dimensionality == 768
    assert captured["config"].auto_truncate is True
    assert captured["contents"] == ["how do I reset my password?"]
    assert len(out) == 768
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/compat/test_kb_embeddings.py::test_embed_query_sync_uses_retrieval_query_task_type -v`
Expected: FAIL with `AttributeError: ... has no attribute '_embed_query_sync'`.

- [ ] **Step 3: Implement `_embed_query_sync` + `embed_query`**

Append to `apps/api/src/usan_api/compat/kb_embeddings.py`:

```python
def _embed_query_sync(text: str, settings: Settings) -> list[float]:
    client = genai.Client(
        vertexai=True, project=settings.gcp_project, location=settings.kb_embedding_location
    )
    # RETRIEVAL_QUERY: asymmetric retrieval — the query is embedded differently from the stored
    # documents (which used RETRIEVAL_DOCUMENT). auto_truncate guards an over-long query turn.
    config = types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY", output_dimensionality=_DIM, auto_truncate=True
    )
    try:
        resp = client.models.embed_content(
            model=settings.kb_embedding_model, contents=[text], config=config
        )
        embeddings = resp.embeddings or []
        return list(embeddings[0].values or []) if embeddings else []
    finally:
        client.close()


async def embed_query(text: str, settings: Settings) -> list[float]:
    """Embed one query string -> a 768-dim vector. Raises ValueError on an unexpected shape."""
    vector = await asyncio.to_thread(_embed_query_sync, text, settings)
    if len(vector) != _DIM:
        logger.bind(n_out=len(vector), model=settings.kb_embedding_model).error(
            "KB query embedding returned unexpected shape model={model}"
        )
        raise ValueError("query embedding shape mismatch")
    return vector
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/compat/test_kb_embeddings.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/compat/kb_embeddings.py tests/compat/test_kb_embeddings.py
git commit -m "feat(api): Phase 5b — add embed_query (RETRIEVAL_QUERY)"
```

---

### Task 4: Vector search repo (`search_chunks`)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/knowledge_bases.py`
- Test: `apps/api/tests/test_knowledge_bases_repo.py` (extend)

**Interfaces:**
- Consumes: `KnowledgeBaseChunk` (ORM, `embedding Vector(768)`).
- Produces: `ChunkHit` (frozen dataclass: `knowledge_base_id: uuid.UUID`, `content: str`, `distance: float`); `async def search_chunks(db, *, kb_ids: list[uuid.UUID], query_embedding: list[float], limit: int, max_distance: float) -> list[ChunkHit]`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/api/tests/test_knowledge_bases_repo.py`. Reuse the existing imports (`uuid`, `text`, `create_async_engine`, `NullPool`, `set_tenant_context`, `repo`, the `two_orgs`/`app_session`/`async_database_url` fixtures, and the `_delete_kbs_for_org` teardown helper). Add a superuser seed helper for a KB + source + chunk with a chosen embedding:

```python
def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


async def _seed_chunk_for_org(
    super_url: str, org_id: uuid.UUID, *, embedding: list[float], content: str
) -> uuid.UUID:
    """Insert a KB + source + one chunk (superuser bypasses RLS); return the KB id."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            kb_id = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases "
                        "(organization_id, name, status, max_chunk_size, min_chunk_size) "
                        "VALUES (:org, 'kb', 'complete', 2000, 400) RETURNING id"
                    ),
                    {"org": str(org_id)},
                )
            ).scalar_one()
            src_id = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_base_sources "
                        "(organization_id, knowledge_base_id, source_type, content, content_url) "
                        "VALUES (:org, :kb, 'text', :c, 'x') RETURNING id"
                    ),
                    {"org": str(org_id), "kb": str(kb_id), "c": content},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO knowledge_base_chunks "
                    "(organization_id, knowledge_base_id, source_id, chunk_index, content, embedding) "
                    "VALUES (:org, :kb, :src, 0, :c, CAST(:emb AS vector))"
                ),
                {
                    "org": str(org_id),
                    "kb": str(kb_id),
                    "src": str(src_id),
                    "c": content,
                    "emb": _vec_literal(embedding),
                },
            )
            return kb_id
    finally:
        await engine.dispose()


def _unit(i: int, dim: int = 768) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


@pytest.mark.asyncio
async def test_search_chunks_orders_by_distance_and_applies_floor(
    two_orgs, app_session, async_database_url
) -> None:
    org_a, _org_b = two_orgs
    near = await _seed_chunk_for_org(async_database_url, org_a, embedding=_unit(0), content="near")
    far = await _seed_chunk_for_org(async_database_url, org_a, embedding=_unit(1), content="far")
    try:
        await set_tenant_context(app_session, org_a)
        # query == _unit(0): distance 0 to `near`, ~1.0 to `far`.
        hits = await repo.search_chunks(
            app_session,
            kb_ids=[near, far],
            query_embedding=_unit(0),
            limit=10,
            max_distance=2.0,
        )
        assert [h.content for h in hits] == ["near", "far"]  # ascending distance
        assert hits[0].distance < hits[1].distance
        # floor drops the far chunk
        floored = await repo.search_chunks(
            app_session, kb_ids=[near, far], query_embedding=_unit(0), limit=10, max_distance=0.5
        )
        assert [h.content for h in floored] == ["near"]
        # limit caps results
        limited = await repo.search_chunks(
            app_session, kb_ids=[near, far], query_embedding=_unit(0), limit=1, max_distance=2.0
        )
        assert len(limited) == 1
        # empty kb_ids -> no query
        assert await repo.search_chunks(
            app_session, kb_ids=[], query_embedding=_unit(0), limit=10, max_distance=2.0
        ) == []
    finally:
        await _delete_kbs_for_org(async_database_url, org_a)


@pytest.mark.asyncio
async def test_search_chunks_cross_org_isolation(
    two_orgs, app_session, async_database_url
) -> None:
    org_a, org_b = two_orgs
    kb_b = await _seed_chunk_for_org(async_database_url, org_b, embedding=_unit(0), content="b")
    try:
        await set_tenant_context(app_session, org_a)
        # org A passing org B's kb_id sees nothing (RLS: usan_app is always policy-bound).
        hits = await repo.search_chunks(
            app_session, kb_ids=[kb_b], query_embedding=_unit(0), limit=10, max_distance=2.0
        )
        assert hits == []
    finally:
        await _delete_kbs_for_org(async_database_url, org_b)
```

If `_delete_kbs_for_org` does not already delete chunks/sources, confirm it removes `knowledge_base_chunks` + `knowledge_base_sources` for the org first (the FK to `organizations` has no `ON DELETE CASCADE`); extend it if needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_knowledge_bases_repo.py -k search_chunks -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'search_chunks'`.

- [ ] **Step 3: Implement `ChunkHit` + `search_chunks`**

In `apps/api/src/usan_api/repositories/knowledge_bases.py`: add `from dataclasses import dataclass` to the imports, and append:

```python
@dataclass(frozen=True)
class ChunkHit:
    knowledge_base_id: uuid.UUID
    content: str
    distance: float


async def search_chunks(
    db: AsyncSession,
    *,
    kb_ids: list[uuid.UUID],
    query_embedding: list[float],
    limit: int,
    max_distance: float,
) -> list[ChunkHit]:
    """RLS-scoped cosine-distance search over the bound KBs' chunks. Returns hits ordered by
    ascending distance, capped at `limit`, with hits above `max_distance` dropped (the relevance
    floor). Empty `kb_ids` -> [] (no query). The embedding binds as a pgvector parameter."""
    if not kb_ids:
        return []
    distance = KnowledgeBaseChunk.embedding.cosine_distance(query_embedding).label("distance")
    rows = (
        await db.execute(
            select(
                KnowledgeBaseChunk.knowledge_base_id,
                KnowledgeBaseChunk.content,
                distance,
            )
            .where(KnowledgeBaseChunk.knowledge_base_id.in_(kb_ids))
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        ChunkHit(knowledge_base_id=r[0], content=r[1], distance=float(r[2]))
        for r in rows
        if float(r[2]) <= max_distance
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_knowledge_bases_repo.py -k search_chunks -v`
Expected: PASS (both).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/repositories/knowledge_bases.py tests/test_knowledge_bases_repo.py
git commit -m "feat(api): Phase 5b — RLS-scoped search_chunks (cosine top-k + floor)"
```

---

### Task 5: Retrieval orchestration (`compat/kb_retrieval.py`)

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_retrieval.py`
- Test: `apps/api/tests/compat/test_kb_retrieval.py`

**Interfaces:**
- Consumes: `Settings` (`kb_retrieval_enabled`, `gcp_project`, `kb_retrieval_top_k`, `kb_retrieval_max_distance`, `kb_retrieval_max_context_chars`), `embed_query` (Task 3), `search_chunks`/`ChunkHit` (Task 4), `ids.decode_kb_id`.
- Produces: `RetrievedContext` (frozen dataclass: `text: str`, `hit_count: int`); `async def retrieve_context(db, settings, *, kb_ids: list[str], query: str) -> RetrievedContext`. May raise (embed/search failures) — the chat-service caller (Task 7) wraps it and degrades.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/compat/test_kb_retrieval.py`:

```python
"""Phase 5b — retrieve_context gating, assembly, char-cap, PHI-safe logging."""

from __future__ import annotations

import uuid

import pytest

from usan_api.compat import ids, kb_retrieval
from usan_api.compat.kb_retrieval import RetrievedContext, retrieve_context
from usan_api.repositories.knowledge_bases import ChunkHit
from usan_api.settings import Settings


def _settings(**over) -> Settings:
    base = {"kb_retrieval_enabled": True, "gcp_project": "test-project"}
    base.update(over)
    return Settings(**base)


_KB = ids.encode_kb_id(uuid.uuid4())


@pytest.mark.asyncio
async def test_gating_returns_empty(monkeypatch) -> None:
    async def _boom(*a, **k):  # must never be called when gated
        raise AssertionError("embed_query called while gated")

    monkeypatch.setattr(kb_retrieval, "embed_query", _boom)

    # flag off
    r = await retrieve_context(None, _settings(kb_retrieval_enabled=False), kb_ids=[_KB], query="q")
    assert r == RetrievedContext("", 0)
    # no gcp_project
    r = await retrieve_context(None, _settings(gcp_project=None), kb_ids=[_KB], query="q")
    assert r == RetrievedContext("", 0)
    # no kb_ids
    r = await retrieve_context(None, _settings(), kb_ids=[], query="q")
    assert r == RetrievedContext("", 0)
    # blank query
    r = await retrieve_context(None, _settings(), kb_ids=[_KB], query="   ")
    assert r == RetrievedContext("", 0)


@pytest.mark.asyncio
async def test_assembles_block_and_counts(monkeypatch) -> None:
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="alpha", distance=0.1),
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="beta", distance=0.2),
        ]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)
    r = await retrieve_context(None, _settings(), kb_ids=[_KB], query="q")
    assert r.hit_count == 2
    assert "alpha" in r.text and "beta" in r.text


@pytest.mark.asyncio
async def test_char_cap_stops_before_exceeding(monkeypatch) -> None:
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="A" * 50, distance=0.1),
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="B" * 50, distance=0.2),
        ]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)
    r = await retrieve_context(
        None, _settings(kb_retrieval_max_context_chars=60), kb_ids=[_KB], query="q"
    )
    assert "A" * 50 in r.text  # first chunk fits
    assert "B" * 50 not in r.text  # second would exceed 60 -> dropped
    assert len(r.text) <= 60


@pytest.mark.asyncio
async def test_logs_are_phi_safe(monkeypatch, caplog) -> None:
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [ChunkHit(knowledge_base_id=uuid.uuid4(), content="SECRET_PHI_TEXT", distance=0.1)]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)
    with caplog.at_level("DEBUG"):
        await retrieve_context(None, _settings(), kb_ids=[_KB], query="SECRET_QUERY_TEXT")
    blob = caplog.text
    assert "SECRET_PHI_TEXT" not in blob
    assert "SECRET_QUERY_TEXT" not in blob
```

Note: loguru does not propagate to `caplog` by default. If `test_logs_are_phi_safe` cannot capture via `caplog`, follow the project's existing loguru-capture pattern (search tests for `caplog`/`loguru`); if none exists, add a `loguru` sink to a `StringIO` inside the test and assert on its contents. Do not weaken the assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_kb_retrieval.py -v`
Expected: FAIL with `ModuleNotFoundError: ... kb_retrieval`.

- [ ] **Step 3: Implement `kb_retrieval.py`**

Create `apps/api/src/usan_api/compat/kb_retrieval.py`:

```python
"""KB text-RAG retrieval orchestration (Phase 5b). Gates, embeds the query, runs the RLS-scoped
vector search, and assembles a bounded context block. PHI/secret-safe: logs counts + bucketed
distances only — never chunk text, query text, titles, or ids. Embed/search failures propagate;
the chat-service caller wraps this and degrades to a no-context reply."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.kb_embeddings import embed_query
from usan_api.repositories.knowledge_bases import search_chunks
from usan_api.settings import Settings


@dataclass(frozen=True)
class RetrievedContext:
    text: str
    hit_count: int


_EMPTY = RetrievedContext("", 0)


def _assemble(contents: list[str], max_chars: int) -> str:
    """Join chunk contents with a blank-line separator, stopping before exceeding max_chars.
    A single first chunk longer than the cap is truncated (a hit is never silently dropped)."""
    parts: list[str] = []
    total = 0
    for piece in contents:
        if not parts and len(piece) > max_chars:
            parts.append(piece[:max_chars])
            break
        sep = 2 if parts else 0  # cost of the "\n\n" join
        if total + sep + len(piece) > max_chars:
            break
        parts.append(piece)
        total += sep + len(piece)
    return "\n\n".join(parts)


async def retrieve_context(
    db: AsyncSession, settings: Settings, *, kb_ids: list[str], query: str
) -> RetrievedContext:
    if not settings.kb_retrieval_enabled or not settings.gcp_project or not kb_ids:
        return _EMPTY
    if not query.strip():
        return _EMPTY
    kb_uuids = []
    for token in kb_ids:
        try:
            kb_uuids.append(ids.decode_kb_id(token))
        except CompatError:
            continue  # defensive: ids were validated at bind, but never 500 here
    if not kb_uuids:
        return _EMPTY
    vector = await embed_query(query, settings)
    hits = await search_chunks(
        db,
        kb_ids=kb_uuids,
        query_embedding=vector,
        limit=settings.kb_retrieval_top_k,
        max_distance=settings.kb_retrieval_max_distance,
    )
    text = _assemble([h.content for h in hits], settings.kb_retrieval_max_context_chars)
    nearest = round(hits[0].distance, 2) if hits else None
    logger.bind(kb_count=len(kb_uuids), hits=len(hits), nearest=nearest).debug(
        "kb retrieval kb_count={kb_count} hits={hits} nearest={nearest}"
    )
    return RetrievedContext(text=text, hit_count=len(hits))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_kb_retrieval.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/compat/kb_retrieval.py tests/compat/test_kb_retrieval.py
git commit -m "feat(api): Phase 5b — kb_retrieval orchestration (gate, embed, search, assemble)"
```

---

### Task 6: Bind + validate `knowledge_base_ids` on retell-llm

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/retell_llm.py:18-45` (add typed field to both request models)
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (`_apply_llm_overlay`, new `_validate_kb_ids`, `create_response_engine`, `update_response_engine`)
- Test: `apps/api/tests/compat/test_agent_bridge.py` (or the existing retell-llm bridge test file)

**Interfaces:**
- Consumes: `LLMConfig.knowledge_base_ids` (Task 2), `ids.decode_kb_id`, `knowledge_bases.get_kb` (existing repo fn).
- Produces: `CreateRetellLlmRequest.knowledge_base_ids` / `UpdateRetellLlmRequest.knowledge_base_ids` (`list[str] | None`); native `config["llm"]["knowledge_base_ids"]` written on create/update-retell-llm; 422 on unknown/cross-org/malformed.

- [ ] **Step 1: Write the failing tests**

Add to the retell-llm bridge test file (search `tests/compat/` for the file exercising `create_response_engine` / `update_response_engine`; if it lives in `test_agent_bridge.py`, add there). Use the established `app_session` + `set_tenant_context` patterns; create a real KB via the repo so a valid id exists. Add the imports these tests need: `uuid`, `from sqlalchemy import text`, `from usan_api.compat.errors import CompatError`, `from usan_api.compat.ids import encode_kb_id`, `from usan_api.tenant_context import set_tenant_context`, the `two_orgs`/`app_session` fixtures, a local `_settings()` returning `Settings(gcp_project="test-project")`, and `_seed_kb_for_org`/`_delete_kbs_for_org` (replicate locally if cross-module import causes collection issues).

```python
@pytest.mark.asyncio
async def test_create_retell_llm_rejects_unknown_kb_id(app_session) -> None:
    from usan_api.compat import agent_bridge
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    body = CreateRetellLlmRequest(
        general_prompt="hi", knowledge_base_ids=[encode_kb_id(uuid.uuid4())]  # well-formed, absent
    )
    with pytest.raises(CompatError) as ei:
        await agent_bridge.create_response_engine(app_session, _settings(), body)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_retell_llm_rejects_malformed_kb_id(app_session) -> None:
    from usan_api.compat import agent_bridge
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    body = CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=["not-a-kb-id"])
    with pytest.raises(CompatError) as ei:
        await agent_bridge.create_response_engine(app_session, _settings(), body)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_retell_llm_rejects_cross_org_kb_id(
    two_orgs, app_session, async_database_url
) -> None:
    from usan_api.compat import agent_bridge
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_a, org_b = two_orgs
    kb_b = await _seed_kb_for_org(async_database_url, org_b, "kb-b")
    try:
        await set_tenant_context(app_session, org_a)
        body = CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=[encode_kb_id(kb_b)])
        with pytest.raises(CompatError) as ei:
            await agent_bridge.create_response_engine(app_session, _settings(), body)
        assert ei.value.status_code == 422
    finally:
        await _delete_kbs_for_org(async_database_url, org_b)


@pytest.mark.asyncio
async def test_create_retell_llm_persists_and_echoes_valid_kb_id(app_session) -> None:
    from usan_api.compat import agent_bridge
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest
    from usan_api.repositories import knowledge_bases as kb_repo

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    kb = await kb_repo.create_kb(
        app_session, name="kb", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    await app_session.commit()
    await set_tenant_context(app_session, org_id)  # commit cleared the is_local context
    kb_token = encode_kb_id(kb.id)
    profile = await agent_bridge.create_response_engine(
        app_session,
        _settings(),
        CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=[kb_token]),
    )
    # native config feeds generation
    assert profile.draft_config["llm"]["knowledge_base_ids"] == [kb_token]
    # echo path unchanged
    resp = agent_bridge.serialize_llm(profile)
    assert resp.model_dump().get("knowledge_base_ids") == [kb_token]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_agent_bridge.py -k retell_llm -v`
Expected: FAIL — `CreateRetellLlmRequest` has no `knowledge_base_ids` attribute / no validation raised.

- [ ] **Step 3a: Add the typed request field**

In `apps/api/src/usan_api/compat/schemas/retell_llm.py`, add to `CreateRetellLlmRequest` (after `model_temperature`, line 31) and `UpdateRetellLlmRequest` (after `model_temperature`, line 45):

```python
    knowledge_base_ids: list[str] | None = None
```

- [ ] **Step 3b: Wire the overlay + validation in `agent_bridge.py`**

Add the import near the other repo imports (line 52 area):

```python
from usan_api.repositories import knowledge_bases as kb_repo
```

Change `_apply_llm_overlay` (lines 69-80) to accept and write the ids:

```python
def _apply_llm_overlay(
    config: dict[str, Any],
    *,
    general_prompt: str | None,
    begin_message: str | None,
    knowledge_base_ids: list[str] | None = None,
) -> None:
    # ``model`` / ``model_temperature`` / ``s2s_model`` are intentionally NOT applied — the
    # prompt runs on the engine's own Vertex pipeline with engine-controlled sampling
    # (data-model §5, Constitution II). They are still echoed to the CRM via compat_extras,
    # just never honored.
    prompts = config["prompts"]
    if general_prompt is not None:
        prompts["system_prompt"] = general_prompt
    if begin_message is not None:
        prompts["greeting"] = begin_message
    # Phase 5b: knowledge_base_ids ARE honored — written into native config["llm"] so chat
    # generation reads cfg.llm.knowledge_base_ids. None = PATCH no-op (prior binding survives).
    if knowledge_base_ids is not None:
        config["llm"]["knowledge_base_ids"] = knowledge_base_ids
```

Add a validation helper (place near `_validate_config`):

```python
async def _validate_kb_ids(db: AsyncSession, kb_ids: list[str] | None) -> None:
    """Reject any knowledge_base_id that doesn't resolve within the caller's org (RLS). Cross-org
    is indistinguishable from absent -> a generic 422 that never acknowledges cross-org
    existence (the same id under another org simply returns None)."""
    for token in kb_ids or []:
        try:
            kb_uuid = ids.decode_kb_id(token)
        except CompatError as exc:
            raise CompatError(422, "unknown knowledge_base_id") from exc
        if await kb_repo.get_kb(db, kb_uuid) is None:
            raise CompatError(422, "unknown knowledge_base_id")
```

In `create_response_engine` (lines 174-194), validate first, then pass the ids to the overlay (replace the `name`/`config`/`_apply_llm_overlay` lines):

```python
    name = await _unique_name(db, _provisional_llm_name())
    await _validate_kb_ids(db, body.knowledge_base_ids)
    config = DEFAULT_AGENT_CONFIG.model_dump()
    _apply_llm_overlay(
        config,
        general_prompt=body.general_prompt,
        begin_message=body.begin_message,
        knowledge_base_ids=body.knowledge_base_ids,
    )
    _merge_extras(config, "llm", body.model_dump())
```

In `update_response_engine` (lines 265-280), the same — validate then overlay (replace the `profile`/`config`/`_apply_llm_overlay` lines):

```python
    profile = await _load_active(db, ids.decode_llm_id(llm_id), kind="response engine")
    await _validate_kb_ids(db, body.knowledge_base_ids)
    config = _config_dict(profile)
    _apply_llm_overlay(
        config,
        general_prompt=body.general_prompt,
        begin_message=body.begin_message,
        knowledge_base_ids=body.knowledge_base_ids,
    )
    _merge_extras(config, "llm", body.model_dump(exclude_none=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_agent_bridge.py -k retell_llm -v`
Expected: PASS (all four).

- [ ] **Step 5: Verify the freeze + SDK round-trip stay green**

Run: `uv run pytest tests/compat/test_freeze_agents.py tests/compat/test_freeze_chat_agents.py -v`
Expected: PASS (unchanged — the new field defaults to absent on those payloads; the echo path is additive).

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/compat/schemas/retell_llm.py src/usan_api/compat/agent_bridge.py tests/compat/test_agent_bridge.py
git commit -m "feat(api): Phase 5b — bind+validate knowledge_base_ids on retell-llm (422 unknown/cross-org)"
```

---

### Task 7: Inject retrieved context at the chat chokepoint

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (imports + `generate_agent_reply`, lines 193-216)
- Test: `apps/api/tests/compat/test_chat_service.py` (extend)

**Interfaces:**
- Consumes: `retrieve_context` / `RetrievedContext` (Task 5), `cfg.llm.knowledge_base_ids` (Task 2), `settings.kb_retrieval_enabled` (Task 1).
- Produces: `generate_agent_reply` augments `system_instruction` with retrieved context for both text channels; never raises from retrieval.

- [ ] **Step 1: Write the failing tests**

Add to `apps/api/tests/compat/test_chat_service.py`. These patch `chat_service.retrieve_context` (the injection logic is under test, not the retrieval internals) and capture the `system_instruction` passed to the mocked `run_vertex_turn`. Write a `_seed_session_with_kb(db, org_id, *, kb_ids, user_text)` helper near the existing `_seed_published_profile` / `_seed_chat` helpers: deep-copy `_VALID_CONFIG`, set `cfg["llm"]["knowledge_base_ids"] = kb_ids`, build a published `AgentProfile` + `AgentProfileVersion` from it (as `_seed_published_profile` does), create a `ChatSession` for `org_id` pointing at the profile, add one `ChatMessage(role="user", content=user_text)` via `chats_repo`, flush, and return the session.

```python
@pytest.mark.asyncio
async def test_generate_agent_reply_injects_kb_context(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.compat.kb_retrieval import RetrievedContext
    from usan_api.vertex_test import VertexTurn

    captured = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def fake_retrieve(db, settings, *, kb_ids, query):
        captured["query"] = query
        return RetrievedContext(text="DOC_CONTEXT", hit_count=1)

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", fake_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="my question"
    )

    reply = await chat_service.generate_agent_reply(app_session, settings, session)
    assert reply == "answer"
    assert "DOC_CONTEXT" in captured["system_instruction"]
    assert "Knowledge base context:" in captured["system_instruction"]
    assert captured["query"] == "my question"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_generate_agent_reply_no_kb_context_when_no_match(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.compat.kb_retrieval import RetrievedContext
    from usan_api.vertex_test import VertexTurn

    captured = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def fake_retrieve(db, settings, *, kb_ids, query):
        return RetrievedContext(text="", hit_count=0)

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", fake_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="q"
    )
    await chat_service.generate_agent_reply(app_session, settings, session)
    assert "Knowledge base context:" not in captured["system_instruction"]
    await app_session.rollback()


@pytest.mark.asyncio
async def test_generate_agent_reply_degrades_on_retrieval_error(app_session, monkeypatch) -> None:
    from usan_api.compat import chat_service
    from usan_api.vertex_test import VertexTurn

    captured = {}

    async def fake_turn(**kwargs):
        captured["system_instruction"] = kwargs["system_instruction"]
        return VertexTurn(text="answer")

    async def boom_retrieve(db, settings, *, kb_ids, query):
        raise RuntimeError("vertex 429")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    monkeypatch.setattr("usan_api.compat.chat_service.retrieve_context", boom_retrieve)
    settings = get_settings().model_copy(
        update={"gcp_project": "test-project", "kb_retrieval_enabled": True}
    )
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    session = await _seed_session_with_kb(
        app_session, org_id, kb_ids=["knowledge_base_abc"], user_text="q"
    )
    reply = await chat_service.generate_agent_reply(app_session, settings, session)
    assert reply == "answer"  # retrieval failure never breaks the reply
    assert "Knowledge base context:" not in captured["system_instruction"]
    await app_session.rollback()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_chat_service.py -k "kb_context or no_match or degrades" -v`
Expected: FAIL — `chat_service` has no `retrieve_context` to patch / no injection.

- [ ] **Step 3: Implement the injection**

In `apps/api/src/usan_api/compat/chat_service.py`, add the import (near line 18-19, with the other `usan_api.compat` imports):

```python
from usan_api.compat.kb_retrieval import RetrievedContext, retrieve_context
```

Replace the body of `generate_agent_reply` (lines 199-216) so the history loads before the system prompt is finalized, and the KB block is appended:

```python
    cfg = await _load_published_config(db, session.agent_profile_id)
    bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
    values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)
    history = await chats_repo.list_messages(db, session.id)

    kb_ids = cfg.llm.knowledge_base_ids or []
    if kb_ids and settings.kb_retrieval_enabled:
        query_text = next(
            (m.content for m in reversed(history) if m.role in ("user", "sms")), ""
        )
        try:
            retrieved = await retrieve_context(db, settings, kb_ids=kb_ids, query=query_text)
        except Exception as exc:  # best-effort: retrieval NEVER breaks a reply
            logger.bind(err=type(exc).__name__, kb_count=len(kb_ids)).warning(
                "kb retrieval failed; replying without context"
            )
            retrieved = RetrievedContext("", 0)
        if retrieved.text:
            system_instruction += (
                "\n\nKnowledge base context:\n"
                + retrieved.text
                + "\n\nUse the above context to answer when relevant."
            )

    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in history
    ]
    turn = await run_vertex_turn(
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        system_instruction=system_instruction,
        tools=[],
        contents=contents,
        settings=settings,
    )
    return turn.text
```

(`logger` is already imported in `chat_service.py` at line 13.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_chat_service.py -v`
Expected: PASS (new injection tests + existing completion tests; both channels share this function so the SMS path inherits the behavior).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add src/usan_api/compat/chat_service.py tests/compat/test_chat_service.py
git commit -m "feat(api): Phase 5b — inject KB context into generate_agent_reply (both text channels)"
```

---

### Task 8: Operator deployment note

**Files:**
- Create: `docs/deployment/text-rag-retrieval.md`

**Interfaces:**
- Consumes: nothing (documentation).
- Produces: operator runbook for activating text-RAG retrieval.

- [ ] **Step 1: Write the note**

Create `docs/deployment/text-rag-retrieval.md`:

````markdown
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
````

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/text-rag-retrieval.md
git commit -m "docs(api): Phase 5b — text-RAG retrieval operator note"
```

---

## Final verification (after all tasks)

- [ ] Full api suite: `cd apps/api && uv run pytest` (parallel; expect green).
- [ ] Lint + types: `ruff check . && ruff format --check . && uv run mypy`.
- [ ] Confirm `KNOWN_GAPS` is still `frozenset()` and no migration was added:
  `git diff --stat main...HEAD` shows no file under `migrations/versions/`.
- [ ] Whole-branch review via superpowers:requesting-code-review, then finish the branch
  (push + PR; squash-merge only on explicit go-ahead; NO `v*` tag).

## Self-review notes (plan author)

- **Spec coverage:** §3 decisions 1-4 -> Tasks 7 (both channels), 4 (floor), 6 (422 bind), 5/7
  (invisible). §5 binding -> Tasks 2+6. §6 pipeline -> Tasks 3,4,5. §7 injection -> Task 7. §8
  settings -> Task 1. §9 PHI/security -> Tasks 5,6,7 (counts-only logs, RLS, degrade). §10 tests
  -> each task's tests. §11 out-of-scope -> respected (no voice, no new response field). No gaps.
- **Type consistency:** `embed_query(text, settings)->list[float]`, `ChunkHit(knowledge_base_id,
  content, distance)`, `search_chunks(db, *, kb_ids, query_embedding, limit, max_distance)`,
  `RetrievedContext(text, hit_count)`, `retrieve_context(db, settings, *, kb_ids, query)`,
  `LLMConfig.knowledge_base_ids`, the 4 `kb_retrieval_*` settings — names match across Tasks 1-7.
- **No placeholders:** every code step shows complete code; test seeds reuse named existing
  helpers/fixtures.
