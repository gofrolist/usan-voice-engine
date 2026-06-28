# Phase 5 — Knowledge Bases (management + async pgvector ingestion) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve RetellAI's 6 knowledge-base CRUD ops conformantly, backed by a durable, multi-tenant, async ingestion pipeline that chunks text sources, embeds them on Vertex (`text-embedding-005`, 768-dim), and stores vectors in pgvector. Ships fully inert.

**Architecture:** New `knowledge_bases`/`knowledge_base_sources`/`knowledge_base_chunks` tables (migration `0047`, owner-DDL, TenantScoped + FORCE-RLS, `CREATE EXTENSION vector`, `Vector(768)` + HNSW). Thin compat handlers persist `status=in_progress`; a flag-gated in-process poller lease-claims KBs **cross-org** via a SECURITY DEFINER function, then processes each under its own org's RLS (`set_tenant_context`, is_local). Retrieval/binding deferred to 5b/5c.

**Tech Stack:** FastAPI sub-app, SQLAlchemy 2 async, alembic, pgvector, `google-genai` (Vertex ADC), pytest (testcontainers PG), retell-sdk 5.53.0 conformance.

**Spec:** `docs/superpowers/specs/2026-06-28-retell-parity-phase5-knowledge-bases-design.md` (esp. §4 data model, §5 ingestion, §12 pinned facts — referenced below as "spec §N").

## Global Constraints

Every task's requirements implicitly include these:

- **Oracle governs shape.** Exact paths verbatim (incl. the singular `/source/` in delete-source); status codes exactly **201 / 201 / 200 / 200 / 204 / 200**. `KnowledgeBaseResponse` required = `knowledge_base_id, knowledge_base_name, status`. Text source variant required = `type, source_id, title, content_url`.
- **Omit-nulls.** Response schemas use `field: T | None = None` (NEVER `default_factory`); every route sets `response_model_exclude_none=True`. `knowledge_base_sources` is **omitted** unless `status=='complete'`.
- **`KNOWN_GAPS` stays `frozenset()`** (`tests/compat/test_surface_coverage.py:26`). Moving a path 501→served requires deleting its tuple from `compat/routers/unsupported.py` (lines 39–44) AND the matching line in `tests/test_compat_fidelity.py` (line 117).
- **`organization_id` is server-set** (TenantScoped column default + RLS WITH CHECK). Repos NEVER set it.
- **Cross-org discovery is ONLY via the SECURITY DEFINER `claim_pending_knowledge_bases` function.** Per-claimed-row processing re-scopes with `set_tenant_context(db, org_id)` (is_local=true, `tenant_context.py:44`). Under `usan_app` a plain cross-org `SELECT` returns nothing (RLS fail-closed) — never rely on it.
- **PHI-safe logging.** `_audit` logs org + op + `knowledge_base_id` only. Ingestion/embedding errors log `type(exc).__name__` + counts/model name only — NEVER source/chunk text, titles, or `error_detail`. `error_detail` is stored internal-only, never serialized.
- **Commit discipline.** `get_compat_db` does NOT autocommit (`compat/auth.py:103`); every mutating service fn ends with `await db.commit()`. Repos are flush-only.
- **`apps/api` ⊥ `services/agent`** — no imports either direction.
- **CI gate (run before every commit):** `cd apps/api && ruff check . && ruff format . && uv run mypy && uv run pytest`. `uv run mypy` uses `files=["src"]` — NEVER `mypy .`.
- **Single alembic head:** `0047`, `down_revision="0046"`. Bump if another migration lands first.
- **Inert ship, no `v*` tag.** All new flags default OFF. Migration is owner-DDL (safe on the owner-runner deploy; would crash-loop a least-priv `usan_app` entrypoint upgrade only if run as `usan_app` — which it isn't, the owner-runner migrates first).
- **Embeddings:** Vertex ADC only (`genai.Client(vertexai=True)`), never the Gemini Developer API; **regional** location (`text-embedding-005` is not served from `global`); off-loop via `asyncio.to_thread`; no-op unless `kb_embedding_enabled and settings.gcp_project`.
- **Text sources only** this phase: `knowledge_base_files` or `knowledge_base_urls` present → **422** (fail-loud).
- **except-paren display artifact:** this env strips parens from `except (A, B):` on display — verify Python syntax via `python -m py_compile`, never by eye.

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `apps/api/pyproject.toml` (+ `uv.lock`) | add `pgvector>=0.3.6` | 1 |
| `apps/api/migrations/versions/0047_knowledge_bases.py` | extension + 3 tables + RLS + HNSW + SECURITY DEFINER claim fn | 1 |
| `apps/api/src/usan_api/db/models.py` | `KnowledgeBase`, `KnowledgeBaseSource`, `KnowledgeBaseChunk` ORM | 1 |
| `apps/api/src/usan_api/repositories/knowledge_bases.py` | KB/source/chunk repos + `claim_pending` | 2 |
| `apps/api/src/usan_api/compat/ids.py` | `knowledge_base_`/`source_` codec | 3 |
| `apps/api/src/usan_api/compat/schemas/knowledge_bases.py` | response + text-source + parsed-request DTOs | 3 |
| `apps/api/src/usan_api/compat/kb_serializer.py` | `serialize_kb(kb, sources)` | 3 |
| `apps/api/src/usan_api/compat/kb_chunking.py` | `chunk_text(content, *, min_size, max_size)` | 4 |
| `apps/api/src/usan_api/compat/kb_embeddings.py` | `embed_texts(texts, settings)` (Vertex) | 4 |
| `apps/api/src/usan_api/settings.py` | `kb_*` fields | 4 |
| `apps/api/src/usan_api/compat/kb_ingestion.py` | `ingest_one_kb(db, kb_id, settings)` | 5 |
| `apps/api/src/usan_api/compat/kb_ingestion_poller.py` | `poll_once` + `run_poller` | 6 |
| `apps/api/src/usan_api/main.py` | lifespan poller wiring | 6 |
| `apps/api/src/usan_api/compat/kb_service.py` | 6 service fns | 7 |
| `apps/api/src/usan_api/compat/routers/knowledge_bases.py` | 6 routes + multipart parse | 8 |
| `apps/api/src/usan_api/compat/routers/unsupported.py` | remove 6 KB tuples | 8 |
| `apps/api/src/usan_api/compat/app.py` | register router | 8 |
| `apps/api/tests/test_compat_fidelity.py` | remove `create-knowledge-base` 501 line | 8 |
| `apps/api/tests/compat/conftest.py` | `mock_embed` fixture | 5 |
| `apps/api/tests/compat/test_freeze_knowledge_bases.py` | conformance freeze | 9 |
| `docs/deployment/knowledge-bases.md` + `infra/docker-compose.yml` + `infra/.env.prod.example` | docs + env passthrough | 10 |

## Interface contract (signatures other tasks rely on)

```python
# repositories/knowledge_bases.py
async def create_kb(db, *, name: str, max_chunk_size: int, min_chunk_size: int, enable_auto_refresh: bool) -> KnowledgeBase
async def get_kb(db, kb_id: uuid.UUID) -> KnowledgeBase | None
async def list_kbs(db) -> list[KnowledgeBase]
async def delete_kb(db, kb_id: uuid.UUID) -> bool
async def set_status(db, kb_id: uuid.UUID, status: str, *, error_detail: str | None = None) -> None  # also clears claimed_at
async def mark_in_progress(db, kb_id: uuid.UUID) -> None  # status='in_progress', claimed_at=NULL (add-sources re-trigger)
async def add_source(db, kb_id: uuid.UUID, *, source_type: str, title: str | None, content: str, content_url: str) -> KnowledgeBaseSource
async def get_sources(db, kb_id: uuid.UUID) -> list[KnowledgeBaseSource]
async def get_sources_for_kbs(db, kb_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[KnowledgeBaseSource]]
async def get_unchunked_sources(db, kb_id: uuid.UUID) -> list[KnowledgeBaseSource]
async def get_source(db, kb_id: uuid.UUID, source_id: uuid.UUID) -> KnowledgeBaseSource | None
async def delete_source(db, source_id: uuid.UUID) -> bool
async def insert_chunks(db, *, kb_id: uuid.UUID, source_id: uuid.UUID, chunks: list[tuple[int, str, list[float]]]) -> None
async def delete_chunks_for_source(db, source_id: uuid.UUID) -> None
async def claim_pending(db, *, limit: int, lease_seconds: int) -> list[tuple[uuid.UUID, uuid.UUID]]  # (kb_id, org_id) via SECURITY DEFINER fn

# compat/ids.py
def encode_kb_id(kb_id: uuid.UUID) -> str          # "knowledge_base_<hex>"
def decode_kb_id(token: str) -> uuid.UUID
def encode_kb_source_id(source_id: uuid.UUID) -> str  # "source_<hex>"
def decode_kb_source_id(token: str) -> uuid.UUID

# compat/schemas/knowledge_bases.py
class KbTextInput(BaseModel): title: str; text: str
@dataclass class ParsedKbCreate: name: str; texts: list[KbTextInput]; has_files: bool; has_urls: bool; enable_auto_refresh: bool; max_chunk_size: int; min_chunk_size: int
@dataclass class ParsedKbAddSources: texts: list[KbTextInput]; has_files: bool; has_urls: bool
class KbTextSource(BaseModel): type, source_id, title, content_url
class KnowledgeBaseResponse(BaseModel): knowledge_base_id; knowledge_base_name; status; (optional) knowledge_base_sources, enable_auto_refresh, max_chunk_size, min_chunk_size

# compat/kb_serializer.py
def serialize_kb(kb: KnowledgeBase, sources: list[KnowledgeBaseSource]) -> KnowledgeBaseResponse

# compat/kb_chunking.py
def chunk_text(content: str, *, min_size: int, max_size: int) -> list[str]

# compat/kb_embeddings.py
async def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]

# compat/kb_ingestion.py
async def ingest_one_kb(db, kb_id: uuid.UUID, settings: Settings) -> None  # org context already set by caller

# compat/kb_ingestion_poller.py
async def poll_once(factory, settings: Settings) -> int
async def run_poller(settings: Settings, stop: asyncio.Event) -> None

# compat/kb_service.py  (all commit; return ORM, router serializes)
async def create_kb(db, parsed: ParsedKbCreate) -> KnowledgeBase
async def add_sources(db, kb_id_token: str, parsed: ParsedKbAddSources) -> KnowledgeBase
async def get_kb(db, kb_id_token: str) -> KnowledgeBase
async def list_kbs(db) -> list[KnowledgeBase]
async def delete_kb(db, kb_id_token: str) -> None
async def delete_source(db, kb_id_token: str, source_id_token: str) -> KnowledgeBase
```

---

### Task 1: pgvector dependency + migration 0047 + ORM models

**Files:**
- Modify: `apps/api/pyproject.toml`, `apps/api/uv.lock`
- Create: `apps/api/migrations/versions/0047_knowledge_bases.py`
- Modify: `apps/api/src/usan_api/db/models.py` (append 3 models)
- Test: `apps/api/tests/test_knowledge_bases_migration.py`

**Interfaces:**
- Produces: tables `knowledge_bases`, `knowledge_base_sources`, `knowledge_base_chunks`; function `claim_pending_knowledge_bases(int, int)`; ORM `KnowledgeBase`, `KnowledgeBaseSource`, `KnowledgeBaseChunk`.

- [ ] **Step 1: Add the dependency**

```bash
cd apps/api && uv add 'pgvector>=0.3.6'
```
Expected: `pyproject.toml` gains `pgvector>=0.3.6` under `[project] dependencies`; `uv.lock` updated.

- [ ] **Step 2: Write the migration** `apps/api/migrations/versions/0047_knowledge_bases.py`

Copy the `_enable_rls` helper + `_ORG_DEFAULT_EXPR` verbatim from `0046_chat_analyses.py` (spec §12). `CREATE EXTENSION` is the FIRST upgrade statement (the `vector` type + `hnsw` AM come from it). The SECURITY DEFINER function is the cross-org claim (spec §4).

```python
"""knowledge bases: 3 TenantScoped + FORCE-RLS tables + pgvector + cross-org claim fn (Phase 5).

Owner-DDL: CREATE EXTENSION vector + FORCE RLS + GRANT usan_app + a SECURITY DEFINER claim
function (the only cross-org primitive — the ingestion poller runs as least-priv usan_app and
cannot SELECT across orgs). Additive + inert until a v* tag.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def _org_col() -> sa.Column:
    return sa.Column(
        "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
    )


def _id_col() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_bases",
        _id_col(),
        _org_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'in_progress'"), nullable=False),
        sa.Column("max_chunk_size", sa.Integer(), nullable=False),
        sa.Column("min_chunk_size", sa.Integer(), nullable=False),
        sa.Column(
            "enable_auto_refresh", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_bases_organization_id", "knowledge_bases", ["organization_id"])
    # Serves the claim predicate (status + lease) without a seq scan.
    op.create_index(
        "ix_knowledge_bases_claim", "knowledge_bases", ["status", "claimed_at", "created_at"]
    )

    op.create_table(
        "knowledge_base_sources",
        _id_col(),
        _org_col(),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_url", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_base_sources_organization_id", "knowledge_base_sources", ["organization_id"]
    )
    op.create_index(
        "ix_knowledge_base_sources_kb", "knowledge_base_sources", ["knowledge_base_id"]
    )

    op.create_table(
        "knowledge_base_chunks",
        _id_col(),
        _org_col(),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["knowledge_base_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_base_chunks_organization_id", "knowledge_base_chunks", ["organization_id"]
    )
    op.create_index("ix_knowledge_base_chunks_source", "knowledge_base_chunks", ["source_id"])
    op.execute(
        "CREATE INDEX ix_knowledge_base_chunks_embedding_hnsw ON knowledge_base_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # Cross-org lease-claim. SECURITY DEFINER (owner) bypasses RLS to see all orgs; returns ids
    # only (no PHI); explicit search_path (definer hygiene). GRANT EXECUTE to usan_app.
    op.execute(
        """
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
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION claim_pending_knowledge_bases(int, int) TO usan_app"
    )

    for t in ("knowledge_bases", "knowledge_base_sources", "knowledge_base_chunks"):
        _enable_rls(t)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_pending_knowledge_bases(int, int)")
    for t in ("knowledge_base_chunks", "knowledge_base_sources", "knowledge_bases"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
    op.drop_table("knowledge_base_chunks")
    op.drop_table("knowledge_base_sources")
    op.drop_table("knowledge_bases")
    # Do NOT drop the vector extension (future objects may depend on it).
```

- [ ] **Step 3: Append the ORM models** to `apps/api/src/usan_api/db/models.py`

Mirror `ChatAnalysisRecord` (Base, TenantScoped — do NOT redeclare `organization_id`). Import `from pgvector.sqlalchemy import Vector` at the top of models.py.

```python
class KnowledgeBase(Base, TenantScoped):
    """A RetellAI knowledge base (Phase 5). status drives the async ingestion lifecycle;
    claimed_at + error_detail are INTERNAL (never serialized)."""

    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_progress'"))
    max_chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    min_chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    enable_auto_refresh: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBaseSource(Base, TenantScoped):
    """One ingested source (text this phase). content is raw PHI-adjacent text, never echoed."""

    __tablename__ = "knowledge_base_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBaseChunk(Base, TenantScoped):
    """A chunk + its Vertex embedding (Phase 5). content is PHI-adjacent, never echoed."""

    __tablename__ = "knowledge_base_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_base_sources.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Write the migration test** `apps/api/tests/test_knowledge_bases_migration.py`

Mirror an existing migration test (e.g. `test_phone_numbers_migration.py`): use the superuser `async_database_url` engine to introspect. Assert: the three tables exist, `vector` extension is installed, RLS is enabled+forced on each, `usan_app` has the table grants + EXECUTE on the function, the HNSW index exists, and `claim_pending_knowledge_bases` exists.

```python
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_kb_tables_and_extension_and_rls(async_database_url: str) -> None:
    async def _check() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                ext = await conn.scalar(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
                assert ext == 1
                for t in ("knowledge_bases", "knowledge_base_sources", "knowledge_base_chunks"):
                    relrowsecurity, relforcerowsecurity = (
                        await conn.execute(
                            text(
                                "SELECT relrowsecurity, relforcerowsecurity "
                                "FROM pg_class WHERE relname = :t"
                            ),
                            {"t": t},
                        )
                    ).one()
                    assert relrowsecurity is True, t
                    assert relforcerowsecurity is True, t
                hnsw = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE indexname = 'ix_knowledge_base_chunks_embedding_hnsw'"
                    )
                )
                assert hnsw == 1
                fn = await conn.scalar(
                    text("SELECT 1 FROM pg_proc WHERE proname = 'claim_pending_knowledge_bases'")
                )
                assert fn == 1
                grant = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.role_table_grants "
                        "WHERE table_name = 'knowledge_bases' AND grantee = 'usan_app' "
                        "AND privilege_type = 'INSERT'"
                    )
                )
                assert grant == 1
        finally:
            await engine.dispose()

    asyncio.run(_check())
```

- [ ] **Step 5: Run + verify**

```bash
cd apps/api && python -m py_compile migrations/versions/0047_knowledge_bases.py src/usan_api/db/models.py
uv run alembic heads          # expect a SINGLE head: 0047
uv run pytest tests/test_knowledge_bases_migration.py -v
ruff check . && ruff format . && uv run mypy
```
Expected: single head `0047`; migration test PASSES; lint/mypy clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/pyproject.toml apps/api/uv.lock apps/api/migrations/versions/0047_knowledge_bases.py apps/api/src/usan_api/db/models.py apps/api/tests/test_knowledge_bases_migration.py
git commit -m "feat(api): Phase 5 KB migration 0047 (pgvector + 3 RLS tables + cross-org claim fn) + ORM"
```

---

### Task 2: repositories/knowledge_bases.py

**Files:**
- Create: `apps/api/src/usan_api/repositories/knowledge_bases.py`
- Test: `apps/api/tests/test_knowledge_bases_repo.py`

**Interfaces:**
- Consumes: the Task 1 ORM models.
- Produces: the repo functions in the Interface contract.

- [ ] **Step 1: Write the repo** (flush-only; never sets `organization_id`; `claim_pending` calls the SECURITY DEFINER fn)

```python
"""knowledge_bases repository (Phase 5). RLS-scoped, org auto-filled. Flush-only — the
caller commits. claim_pending calls the SECURITY DEFINER fn (the only cross-org primitive)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import KnowledgeBase, KnowledgeBaseChunk, KnowledgeBaseSource


async def create_kb(
    db: AsyncSession,
    *,
    name: str,
    max_chunk_size: int,
    min_chunk_size: int,
    enable_auto_refresh: bool,
) -> KnowledgeBase:
    kb = KnowledgeBase(
        name=name,
        status="in_progress",
        max_chunk_size=max_chunk_size,
        min_chunk_size=min_chunk_size,
        enable_auto_refresh=enable_auto_refresh,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    return kb


async def get_kb(db: AsyncSession, kb_id: uuid.UUID) -> KnowledgeBase | None:
    return (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one_or_none()


async def list_kbs(db: AsyncSession) -> list[KnowledgeBase]:
    rows = (
        await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()))
    ).scalars().all()
    return list(rows)


async def delete_kb(db: AsyncSession, kb_id: uuid.UUID) -> bool:
    result = await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    return (result.rowcount or 0) > 0


async def set_status(
    db: AsyncSession, kb_id: uuid.UUID, status: str, *, error_detail: str | None = None
) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = status
    kb.error_detail = error_detail
    kb.claimed_at = None
    await db.flush()


async def mark_in_progress(db: AsyncSession, kb_id: uuid.UUID) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None:
        return
    kb.status = "in_progress"
    kb.claimed_at = None
    await db.flush()


async def add_source(
    db: AsyncSession,
    kb_id: uuid.UUID,
    *,
    source_type: str,
    title: str | None,
    content: str,
    content_url: str,
) -> KnowledgeBaseSource:
    src = KnowledgeBaseSource(
        knowledge_base_id=kb_id,
        source_type=source_type,
        title=title,
        content=content,
        content_url=content_url,
    )
    db.add(src)
    await db.flush()
    await db.refresh(src)
    return src


async def get_sources(db: AsyncSession, kb_id: uuid.UUID) -> list[KnowledgeBaseSource]:
    rows = (
        await db.execute(
            select(KnowledgeBaseSource)
            .where(KnowledgeBaseSource.knowledge_base_id == kb_id)
            .order_by(KnowledgeBaseSource.created_at)
        )
    ).scalars().all()
    return list(rows)


async def get_sources_for_kbs(
    db: AsyncSession, kb_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[KnowledgeBaseSource]]:
    if not kb_ids:
        return {}
    rows = (
        await db.execute(
            select(KnowledgeBaseSource)
            .where(KnowledgeBaseSource.knowledge_base_id.in_(kb_ids))
            .order_by(KnowledgeBaseSource.created_at)
        )
    ).scalars().all()
    out: dict[uuid.UUID, list[KnowledgeBaseSource]] = {kid: [] for kid in kb_ids}
    for r in rows:
        out.setdefault(r.knowledge_base_id, []).append(r)
    return out


async def get_unchunked_sources(
    db: AsyncSession, kb_id: uuid.UUID
) -> list[KnowledgeBaseSource]:
    """Sources with no chunks yet (the ingestion work-list — handles create + add-sources)."""
    sub = select(KnowledgeBaseChunk.source_id).where(
        KnowledgeBaseChunk.source_id == KnowledgeBaseSource.id
    )
    rows = (
        await db.execute(
            select(KnowledgeBaseSource)
            .where(KnowledgeBaseSource.knowledge_base_id == kb_id)
            .where(~sub.exists())
            .order_by(KnowledgeBaseSource.created_at)
        )
    ).scalars().all()
    return list(rows)


async def get_source(
    db: AsyncSession, kb_id: uuid.UUID, source_id: uuid.UUID
) -> KnowledgeBaseSource | None:
    return (
        await db.execute(
            select(KnowledgeBaseSource).where(
                KnowledgeBaseSource.id == source_id,
                KnowledgeBaseSource.knowledge_base_id == kb_id,
            )
        )
    ).scalar_one_or_none()


async def delete_source(db: AsyncSession, source_id: uuid.UUID) -> bool:
    result = await db.execute(
        delete(KnowledgeBaseSource).where(KnowledgeBaseSource.id == source_id)
    )
    return (result.rowcount or 0) > 0


async def delete_chunks_for_source(db: AsyncSession, source_id: uuid.UUID) -> None:
    await db.execute(delete(KnowledgeBaseChunk).where(KnowledgeBaseChunk.source_id == source_id))
    await db.flush()


async def insert_chunks(
    db: AsyncSession,
    *,
    kb_id: uuid.UUID,
    source_id: uuid.UUID,
    chunks: list[tuple[int, str, list[float]]],
) -> None:
    for idx, content, embedding in chunks:
        db.add(
            KnowledgeBaseChunk(
                knowledge_base_id=kb_id,
                source_id=source_id,
                chunk_index=idx,
                content=content,
                embedding=embedding,
            )
        )
    await db.flush()


async def claim_pending(
    db: AsyncSession, *, limit: int, lease_seconds: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Lease-claim up to `limit` in_progress KBs across ALL orgs via the SECURITY DEFINER fn.
    Returns (kb_id, org_id) pairs. The caller commits, then processes each under its org."""
    rows = (
        await db.execute(
            text("SELECT id, organization_id FROM claim_pending_knowledge_bases(:lim, :lease)"),
            {"lim": limit, "lease": lease_seconds},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]
```

- [ ] **Step 2: Write the repo test** `apps/api/tests/test_knowledge_bases_repo.py`

Use the `app_session` fixture (usan_app, RLS-enforced) + `set_tenant_context`. For cross-org, seed org B via the superuser `async_database_url` engine (bypasses RLS) — mirror `test_compat_rls_isolation.py`. Assert: CRUD round-trips; a `Vector(768)` list round-trips; cross-org isolation (org A can't see org B's KB); `get_unchunked_sources` excludes chunked sources; `claim_pending` under usan_app returns BOTH orgs' rows (proves the SECURITY DEFINER bypass) while a plain `get_kb` for org B's id under org-A context returns None (proves RLS + the function's necessity).

```python
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


async def _seed_kb_for_org(super_url: str, org_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert a KB directly (superuser bypasses RLS) for an arbitrary org; returns its id."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases "
                        "(organization_id, name, status, max_chunk_size, min_chunk_size) "
                        "VALUES (:org, :name, 'in_progress', 2000, 400) RETURNING id"
                    ),
                    {"org": str(org_id), "name": name},
                )
            ).one()
            return row[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_get_list_delete(app_session) -> None:
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb = await repo.create_kb(
        app_session, name="kb1", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    await app_session.commit()
    await set_tenant_context(app_session, org)
    got = await repo.get_kb(app_session, kb.id)
    assert got is not None and got.name == "kb1" and got.status == "in_progress"
    assert kb.id in {k.id for k in await repo.list_kbs(app_session)}
    assert await repo.delete_kb(app_session, kb.id) is True


@pytest.mark.asyncio
async def test_chunk_vector_roundtrip_and_unchunked(app_session) -> None:
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb = await repo.create_kb(
        app_session, name="kb", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    src = await repo.add_source(
        app_session, kb.id, source_type="text", title="t", content="body", content_url="u"
    )
    assert [s.id for s in await repo.get_unchunked_sources(app_session, kb.id)] == [src.id]
    await repo.insert_chunks(
        app_session, kb_id=kb.id, source_id=src.id, chunks=[(0, "body", [0.1] * 768)]
    )
    assert await repo.get_unchunked_sources(app_session, kb.id) == []


@pytest.mark.asyncio
async def test_cross_org_isolation_and_claim(app_session, async_database_url) -> None:
    org_a = await resolve_default_org_id(app_session)
    # Seed a second org + a pending KB in it, directly (superuser).
    other = uuid.uuid4()
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) "
                    "VALUES (:id, :slug, :name)"
                ),
                {"id": str(other), "slug": f"org-{other.hex[:8]}", "name": "Org B"},
            )
    finally:
        await engine.dispose()
    kb_b = await _seed_kb_for_org(async_database_url, other, "kb-b")

    await set_tenant_context(app_session, org_a)
    # Org A cannot see org B's KB (RLS) — proves the cross-org SELECT is fail-closed.
    assert await repo.get_kb(app_session, kb_b) is None
    # The SECURITY DEFINER claim DOES see org B's pending KB even under org-A context.
    claimed = await repo.claim_pending(app_session, limit=50, lease_seconds=300)
    await app_session.commit()
    assert other in {org for (_kid, org) in claimed}
    assert kb_b in {kid for (kid, _org) in claimed}
```
(NOTE for the implementer: adjust the `organizations` INSERT columns to the real schema — read `db/models.py` Organization; the seed must satisfy NOT NULLs. If a second-org factory fixture already exists in `tests/conftest.py` (grep `Org B` / `second org`), prefer it.)

- [ ] **Step 2b: Run, iterate to green**

```bash
cd apps/api && uv run pytest tests/test_knowledge_bases_repo.py -v
ruff check . && ruff format . && uv run mypy
```

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/repositories/knowledge_bases.py apps/api/tests/test_knowledge_bases_repo.py
git commit -m "feat(api): Phase 5 KB repositories (RLS-scoped CRUD + cross-org SECURITY DEFINER claim)"
```

---

### Task 3: ids codec + schemas + serializer

**Files:**
- Modify: `apps/api/src/usan_api/compat/ids.py`
- Create: `apps/api/src/usan_api/compat/schemas/knowledge_bases.py`
- Create: `apps/api/src/usan_api/compat/kb_serializer.py`
- Test: `apps/api/tests/compat/test_kb_serializer.py`

**Interfaces:**
- Consumes: Task 1 ORM.
- Produces: `encode_kb_id/decode_kb_id`, `encode_kb_source_id/decode_kb_source_id`, the schema classes, `serialize_kb`.

- [ ] **Step 1: Extend `ids.py`** — add two prefixes + two codec pairs (mirror `encode_chat_id`/`decode_chat_id` + `_decode_hex`):

```python
_KB_PREFIX = "knowledge_base_"
_KB_SOURCE_PREFIX = "source_"


def encode_kb_id(kb_id: uuid.UUID) -> str:
    return _KB_PREFIX + kb_id.hex


def decode_kb_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_KB_PREFIX, kind="knowledge_base_id")


def encode_kb_source_id(source_id: uuid.UUID) -> str:
    return _KB_SOURCE_PREFIX + source_id.hex


def decode_kb_source_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_KB_SOURCE_PREFIX, kind="source_id")
```

- [ ] **Step 2: Write the schemas** `apps/api/src/usan_api/compat/schemas/knowledge_bases.py`

```python
"""RetellAI knowledge-base compat schemas (Phase 5). Responses omit None via the route's
response_model_exclude_none=True. Parsed* are the multipart-decoded request DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict


class KbTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    text: str


class KbTextSource(BaseModel):
    type: Literal["text"] = "text"
    source_id: str
    title: str
    content_url: str


class KnowledgeBaseResponse(BaseModel):
    knowledge_base_id: str
    knowledge_base_name: str
    status: str
    knowledge_base_sources: list[KbTextSource] | None = None
    enable_auto_refresh: bool | None = None
    max_chunk_size: int | None = None
    min_chunk_size: int | None = None


@dataclass
class ParsedKbCreate:
    name: str
    texts: list[KbTextInput] = field(default_factory=list)
    has_files: bool = False
    has_urls: bool = False
    enable_auto_refresh: bool = False
    max_chunk_size: int = 2000
    min_chunk_size: int = 400


@dataclass
class ParsedKbAddSources:
    texts: list[KbTextInput] = field(default_factory=list)
    has_files: bool = False
    has_urls: bool = False
```

- [ ] **Step 3: Write the serializer** `apps/api/src/usan_api/compat/kb_serializer.py`

```python
"""ORM -> RetellAI KnowledgeBaseResponse (Phase 5). sources omitted unless status=='complete'."""

from __future__ import annotations

from usan_api.compat import ids
from usan_api.compat.schemas.knowledge_bases import KbTextSource, KnowledgeBaseResponse
from usan_api.db.models import KnowledgeBase, KnowledgeBaseSource


def serialize_kb(
    kb: KnowledgeBase, sources: list[KnowledgeBaseSource]
) -> KnowledgeBaseResponse:
    kb_sources: list[KbTextSource] | None = None
    if kb.status == "complete":
        kb_sources = [
            KbTextSource(
                source_id=ids.encode_kb_source_id(s.id),
                title=s.title or "",
                content_url=s.content_url,
            )
            for s in sources
            if s.source_type == "text"
        ]
    return KnowledgeBaseResponse(
        knowledge_base_id=ids.encode_kb_id(kb.id),
        knowledge_base_name=kb.name,
        status=kb.status,
        knowledge_base_sources=kb_sources,
        enable_auto_refresh=kb.enable_auto_refresh,
        max_chunk_size=kb.max_chunk_size,
        min_chunk_size=kb.min_chunk_size,
    )
```

- [ ] **Step 4: Write serializer + ids tests** `apps/api/tests/compat/test_kb_serializer.py`

Build lightweight ORM instances in-memory (no DB) — set `.id`, `.name`, `.status`, etc. directly. Assert conformance via the harness.

```python
import uuid

from usan_api.compat import ids
from usan_api.compat.kb_serializer import serialize_kb
from usan_api.db.models import KnowledgeBase, KnowledgeBaseSource

from .conformance import assert_conforms, assert_sdk_roundtrip


def _kb(status: str) -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.id = uuid.uuid4()
    kb.name = "Support KB"
    kb.status = status
    kb.max_chunk_size = 2000
    kb.min_chunk_size = 400
    kb.enable_auto_refresh = False
    return kb


def _src() -> KnowledgeBaseSource:
    s = KnowledgeBaseSource()
    s.id = uuid.uuid4()
    s.source_type = "text"
    s.title = "FAQ"
    s.content = "secret body"
    s.content_url = "https://internal/kb-source/x"
    return s


def test_in_progress_omits_sources() -> None:
    body = serialize_kb(_kb("in_progress"), [_src()]).model_dump(exclude_none=True)
    assert "knowledge_base_sources" not in body
    assert body["status"] == "in_progress"
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_complete_includes_text_source_and_never_raw_text() -> None:
    body = serialize_kb(_kb("complete"), [_src()]).model_dump(exclude_none=True)
    assert body["knowledge_base_sources"][0]["type"] == "text"
    assert body["knowledge_base_sources"][0]["source_id"].startswith("source_")
    assert "secret body" not in str(body)  # raw content never echoed
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_kb_id_roundtrip_and_bad_prefix() -> None:
    from usan_api.compat.errors import CompatError

    kid = uuid.uuid4()
    assert ids.decode_kb_id(ids.encode_kb_id(kid)) == kid
    sid = uuid.uuid4()
    assert ids.decode_kb_source_id(ids.encode_kb_source_id(sid)) == sid
    try:
        ids.decode_kb_id("agent_" + kid.hex)
    except CompatError as exc:
        assert exc.status_code == 422
    else:  # pragma: no cover
        raise AssertionError("expected CompatError")
```

- [ ] **Step 5: Run + commit**

```bash
cd apps/api && uv run pytest tests/compat/test_kb_serializer.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/ids.py apps/api/src/usan_api/compat/schemas/knowledge_bases.py apps/api/src/usan_api/compat/kb_serializer.py apps/api/tests/compat/test_kb_serializer.py
git commit -m "feat(api): Phase 5 KB ids codec + schemas + serializer (conformant, omit-nulls)"
```

---

### Task 4: chunking + embeddings + settings

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_chunking.py`, `apps/api/src/usan_api/compat/kb_embeddings.py`
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/compat/test_kb_chunking.py`, `apps/api/tests/compat/test_kb_embeddings.py`

**Interfaces:**
- Produces: `chunk_text(content, *, min_size, max_size) -> list[str]`; `embed_texts(texts, settings) -> list[list[float]]`; settings `kb_*` fields.

- [ ] **Step 1: Settings** — add after the `chat_analysis_*` block (`settings.py:246`):

```python
    # Knowledge-base ingestion (Phase 5). All ship-inert: default OFF, so no Vertex embed
    # (spend or PHI egress) and no poller until a deploy enables them AND gcp_project is set.
    # Vertex text-embedding-005 is REGIONAL — kb_embedding_location must be a region, not
    # "global". Dimension 768 is baked into the Vector(768) column (model change -> migration).
    kb_embedding_enabled: bool = Field(default=False, alias="KB_EMBEDDING_ENABLED")
    kb_embedding_model: str = Field(default="text-embedding-005", alias="KB_EMBEDDING_MODEL")
    kb_embedding_location: str = Field(default="us-central1", alias="KB_EMBEDDING_LOCATION")
    kb_ingestion_poller_enabled: bool = Field(
        default=False, alias="KB_INGESTION_POLLER_ENABLED"
    )
    kb_ingestion_poll_interval_s: int = Field(default=15, alias="KB_INGESTION_POLL_INTERVAL_S")
    kb_ingestion_batch_size: int = Field(default=10, alias="KB_INGESTION_BATCH_SIZE")
    kb_ingestion_lease_seconds: int = Field(default=300, alias="KB_INGESTION_LEASE_SECONDS")
```

- [ ] **Step 2: Chunking** `apps/api/src/usan_api/compat/kb_chunking.py`

Character-based splitter honoring `[min_size, max_size]`. Greedy whitespace packing; hard-cap each chunk at `max_size`; prefer to break on whitespace at/after `min_size`.

```python
"""Text chunking for KB ingestion (Phase 5). Char-based, honors [min_size, max_size]."""

from __future__ import annotations


def chunk_text(content: str, *, min_size: int, max_size: int) -> list[str]:
    text = content.strip()
    if not text:
        return []
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_size, n)
        if end < n:
            # Prefer to break on the last whitespace at/after min_size to avoid splitting words.
            window = text[start:end]
            cut = window.rfind(" ")
            if cut >= min_size:
                end = start + cut
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]
```

- [ ] **Step 3: Chunking test** `apps/api/tests/compat/test_kb_chunking.py`

```python
from usan_api.compat.kb_chunking import chunk_text


def test_short_text_one_chunk() -> None:
    assert chunk_text("hello world", min_size=2, max_size=100) == ["hello world"]


def test_empty_is_no_chunks() -> None:
    assert chunk_text("   ", min_size=2, max_size=100) == []


def test_long_text_respects_max() -> None:
    body = " ".join(["word"] * 500)
    chunks = chunk_text(body, min_size=50, max_size=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks) == body.replace(" ", "")
```

- [ ] **Step 4: Embeddings** `apps/api/src/usan_api/compat/kb_embeddings.py` (spec §12 embed call; PHI-safe; off-loop)

```python
"""Vertex text-embedding for KB ingestion (Phase 5). ADC + vertexai=True only — never the
Gemini Developer API. Regional client. Logs model + counts only, never chunk text."""

from __future__ import annotations

import asyncio

from google import genai
from google.genai import types
from loguru import logger

from usan_api.settings import Settings

_DIM = 768


def _embed_sync(texts: list[str], settings: Settings) -> list[list[float]]:
    client = genai.Client(
        vertexai=True, project=settings.gcp_project, location=settings.kb_embedding_location
    )
    try:
        resp = client.models.embed_content(
            model=settings.kb_embedding_model,
            contents=list(texts),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT", output_dimensionality=_DIM
            ),
        )
    finally:
        client.close()
    return [list(e.values or []) for e in (resp.embeddings or [])]


async def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    """Embed chunk texts -> 768-dim vectors (order-preserving). Empty input -> []."""
    if not texts:
        return []
    vectors = await asyncio.to_thread(_embed_sync, texts, settings)
    if len(vectors) != len(texts) or any(len(v) != _DIM for v in vectors):
        logger.bind(n_in=len(texts), n_out=len(vectors), model=settings.kb_embedding_model).error(
            "KB embedding returned unexpected shape model={model}"
        )
        raise ValueError("embedding shape mismatch")
    return vectors
```

- [ ] **Step 5: Embeddings test** `apps/api/tests/compat/test_kb_embeddings.py` — monkeypatch `_embed_sync` (no real Vertex). Assert order-preserving pass-through + shape-guard raise.

```python
import pytest

from usan_api import settings as settings_mod
from usan_api.compat import kb_embeddings


def _settings(**over):
    base = settings_mod.get_settings()
    return base.model_copy(update={"gcp_project": "p", **over})


@pytest.mark.asyncio
async def test_embed_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(
        kb_embeddings, "_embed_sync", lambda texts, s: [[0.0] * 768 for _ in texts]
    )
    out = await kb_embeddings.embed_texts(["a", "b"], _settings())
    assert len(out) == 2 and all(len(v) == 768 for v in out)


@pytest.mark.asyncio
async def test_embed_shape_mismatch_raises(monkeypatch) -> None:
    monkeypatch.setattr(kb_embeddings, "_embed_sync", lambda texts, s: [[0.0] * 10])
    with pytest.raises(ValueError):
        await kb_embeddings.embed_texts(["a"], _settings())


@pytest.mark.asyncio
async def test_embed_empty() -> None:
    assert await kb_embeddings.embed_texts([], _settings()) == []
```

- [ ] **Step 6: Run + commit**

```bash
cd apps/api && uv run pytest tests/compat/test_kb_chunking.py tests/compat/test_kb_embeddings.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/kb_chunking.py apps/api/src/usan_api/compat/kb_embeddings.py apps/api/src/usan_api/settings.py apps/api/tests/compat/test_kb_chunking.py apps/api/tests/compat/test_kb_embeddings.py
git commit -m "feat(api): Phase 5 KB chunking + Vertex embed helper + settings (inert)"
```

---

### Task 5: ingestion core (`ingest_one_kb`)

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_ingestion.py`
- Modify: `apps/api/tests/compat/conftest.py` (add `mock_embed` fixture)
- Test: `apps/api/tests/compat/test_kb_ingestion.py`

**Interfaces:**
- Consumes: Task 2 repo, Task 4 chunking + embeddings.
- Produces: `ingest_one_kb(db, kb_id, settings) -> None` — assumes the caller has set the org context; flushes only (caller commits). Sets `status` complete/error.

- [ ] **Step 1: Write the ingestion core**

```python
"""KB ingestion core (Phase 5). Process ONE knowledge base: chunk + embed its un-chunked
sources, store vectors, set status complete/error. The caller (poller) sets the org context
and commits. PHI-safe: errors log type name only — never source/chunk text."""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat.kb_chunking import chunk_text
from usan_api.compat.kb_embeddings import embed_texts
from usan_api.repositories import knowledge_bases as repo
from usan_api.settings import Settings


async def ingest_one_kb(db: AsyncSession, kb_id: uuid.UUID, settings: Settings) -> None:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        return
    if not (settings.kb_embedding_enabled and settings.gcp_project):
        # Flag/project off — leave it claimed-but-pending (status stays in_progress); the
        # next enabled deploy reclaims it after the lease. Never marks complete without embeds.
        logger.bind(kb_id=str(kb_id)).info("KB ingestion skipped (embedding disabled)")
        return
    try:
        for src in await repo.get_unchunked_sources(db, kb_id):
            pieces = chunk_text(
                src.content, min_size=kb.min_chunk_size, max_size=kb.max_chunk_size
            )
            await repo.delete_chunks_for_source(db, src.id)  # idempotent re-ingest
            if not pieces:
                continue
            vectors = await embed_texts(pieces, settings)
            await repo.insert_chunks(
                db,
                kb_id=kb_id,
                source_id=src.id,
                chunks=list(zip(range(len(pieces)), pieces, vectors, strict=True)),
            )
        await repo.set_status(db, kb_id, "complete")
    except Exception as exc:  # PHI-safe: type name only
        logger.bind(kb_id=str(kb_id), exc_type=type(exc).__name__).error(
            "KB ingestion failed kb={kb_id} exc={exc_type}"
        )
        await repo.set_status(db, kb_id, "error", error_detail=type(exc).__name__)
    await db.flush()
```

- [ ] **Step 2: Add the `mock_embed` fixture** to `apps/api/tests/compat/conftest.py`

```python
@pytest.fixture
def mock_embed(monkeypatch):
    """Stub the Vertex embed so ingestion tests place no real call: returns a 768-vec per text."""

    async def _fake(texts, settings):
        return [[0.1] * 768 for _ in texts]

    # Patch the name bound inside the ingestion module (where it is called).
    monkeypatch.setattr("usan_api.compat.kb_ingestion.embed_texts", _fake)
```

- [ ] **Step 3: Write the ingestion test** `apps/api/tests/compat/test_kb_ingestion.py` (uses `app_session` + `set_tenant_context`; a settings with the flag on)

```python
import pytest

from usan_api import settings as settings_mod
from usan_api.compat import kb_ingestion
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def _on():
    return settings_mod.get_settings().model_copy(
        update={"kb_embedding_enabled": True, "gcp_project": "p"}
    )


async def _seed(app_session, *, content="hello world body"):
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb = await repo.create_kb(
        app_session, name="k", max_chunk_size=2000, min_chunk_size=2, enable_auto_refresh=False
    )
    await repo.add_source(
        app_session, kb.id, source_type="text", title="t", content=content, content_url="u"
    )
    return kb


@pytest.mark.asyncio
async def test_ingest_completes_and_chunks(app_session, mock_embed) -> None:
    kb = await _seed(app_session)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, _on())
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    assert (await repo.get_kb(app_session, kb.id)).status == "complete"
    assert await repo.get_unchunked_sources(app_session, kb.id) == []


@pytest.mark.asyncio
async def test_ingest_embed_failure_sets_error(app_session, monkeypatch) -> None:
    kb = await _seed(app_session)

    async def _boom(texts, settings):
        raise RuntimeError("vertex down")

    monkeypatch.setattr("usan_api.compat.kb_ingestion.embed_texts", _boom)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, _on())
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb2 = await repo.get_kb(app_session, kb.id)
    assert kb2.status == "error" and kb2.error_detail == "RuntimeError"


@pytest.mark.asyncio
async def test_ingest_disabled_is_noop(app_session, mock_embed) -> None:
    kb = await _seed(app_session)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, settings_mod.get_settings())  # flag off
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    assert (await repo.get_kb(app_session, kb.id)).status == "in_progress"
```

- [ ] **Step 4: Run + commit**

```bash
cd apps/api && uv run pytest tests/compat/test_kb_ingestion.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/kb_ingestion.py apps/api/tests/compat/conftest.py apps/api/tests/compat/test_kb_ingestion.py
git commit -m "feat(api): Phase 5 KB ingestion core (chunk+embed+status, PHI-safe, idempotent)"
```

---

### Task 6: ingestion poller + lifespan wiring

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_ingestion_poller.py`
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/compat/test_kb_ingestion_poller.py`

**Interfaces:**
- Consumes: Task 2 `claim_pending`, Task 5 `ingest_one_kb`.
- Produces: `poll_once(factory, settings) -> int` (count processed), `run_poller(settings, stop)`.

- [ ] **Step 1: Write the poller** (mirror `retry_orchestrator.run_poller` + the one-row-per-txn discipline; cross-org claim then per-row `set_tenant_context`)

```python
"""KB ingestion poller (Phase 5). Cross-org: a SECURITY DEFINER claim returns (kb_id, org_id)
across all orgs (the shared usan_app session is otherwise default-org-pinned); each KB is then
processed in its OWN short transaction under set_tenant_context(org_id). The embed call holds
no DB connection. Mirrors retry_orchestrator's loop discipline."""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api.compat import kb_ingestion
from usan_api.db.session import get_session_factory
from usan_api.repositories import knowledge_bases as repo
from usan_api.settings import Settings
from usan_api.tenant_context import set_tenant_context


async def poll_once(factory: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    async with factory() as db:
        claimed = await repo.claim_pending(
            db,
            limit=settings.kb_ingestion_batch_size,
            lease_seconds=settings.kb_ingestion_lease_seconds,
        )
        await db.commit()
    processed = 0
    for kb_id, org_id in claimed:
        async with factory() as db:
            await set_tenant_context(db, org_id)
            await kb_ingestion.ingest_one_kb(db, kb_id, settings)
            await db.commit()
        processed += 1
    return processed


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    log = logger.bind(component="kb_ingestion_poller")
    log.info("KB ingestion poller started (interval={i}s)", i=settings.kb_ingestion_poll_interval_s)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            n = await poll_once(factory, settings)
            if n:
                log.info("Ingested {n} knowledge base(s)", n=n)
        except Exception:
            log.opt(exception=True).error("KB ingestion poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.kb_ingestion_poll_interval_s)
    log.info("KB ingestion poller stopped")
```

- [ ] **Step 2: Wire into `main.py` lifespan** — add after the `scheduler_poller_enabled` block (`main.py:135-140`), and import `kb_ingestion_poller` with the other orchestrator imports:

```python
    if settings.kb_ingestion_poller_enabled:
        poller_tasks.append(
            asyncio.create_task(kb_ingestion_poller.run_poller(settings, stop))
        )
```
(The existing `stop.set()` / `task.cancel()` / `suppress(asyncio.CancelledError)` shutdown handles it.)

- [ ] **Step 3: Write the poller test** `apps/api/tests/compat/test_kb_ingestion_poller.py` — seed pending KBs in TWO orgs (org A via the app_session; org B via the superuser engine), build a usan_app factory, run `poll_once`, assert BOTH reach `complete`. This is the cross-org test that the SECURITY DEFINER path makes pass.

```python
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import settings as settings_mod
from usan_api.compat import kb_ingestion_poller
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def _on():
    return settings_mod.get_settings().model_copy(
        update={"kb_embedding_enabled": True, "gcp_project": "p", "kb_ingestion_batch_size": 50}
    )


@pytest.mark.asyncio
async def test_poll_processes_two_orgs(
    app_session, app_async_database_url, async_database_url, app_role_password, mock_embed
) -> None:
    # Org A pending KB (default org), seeded under RLS via app_session.
    org_a = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org_a)
    kb_a = await repo.create_kb(
        app_session, name="a", max_chunk_size=2000, min_chunk_size=2, enable_auto_refresh=False
    )
    await repo.add_source(
        app_session, kb_a.id, source_type="text", title="t", content="aaa bbb", content_url="u"
    )
    await app_session.commit()

    # Org B + its pending KB + source, seeded directly (superuser bypasses RLS).
    org_b = uuid.uuid4()
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :s, :n)"),
                {"id": str(org_b), "s": f"org-{org_b.hex[:8]}", "n": "Org B"},
            )
            kb_b = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases (organization_id, name, status, "
                        "max_chunk_size, min_chunk_size) VALUES (:o,'b','in_progress',2000,2) "
                        "RETURNING id"
                    ),
                    {"o": str(org_b)},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO knowledge_base_sources (organization_id, knowledge_base_id, "
                    "source_type, title, content, content_url) "
                    "VALUES (:o, :kb, 'text', 't', 'ccc ddd', 'u')"
                ),
                {"o": str(org_b), "kb": str(kb_b)},
            )
    finally:
        await engine.dispose()

    # Run the poller against a usan_app factory (RLS-subject — proves the cross-org claim).
    factory = async_sessionmaker(
        create_async_engine(app_async_database_url, poolclass=NullPool), expire_on_commit=False
    )
    processed = await kb_ingestion_poller.poll_once(factory, _on())
    assert processed == 2

    # Both KBs reached complete (verify via the superuser engine).
    eng = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            for kid in (kb_a.id, kb_b):
                st = await conn.scalar(
                    text("SELECT status FROM knowledge_bases WHERE id = :id"), {"id": str(kid)}
                )
                assert st == "complete", kid
    finally:
        await eng.dispose()
```
(NOTE for the implementer: confirm the `app_async_database_url` / `app_role_password` fixture names against `tests/conftest.py:145,124`; the poller MUST connect as `usan_app` for this test to actually exercise the SECURITY DEFINER claim. If a usan_app `async_sessionmaker` factory fixture already exists, prefer it. The `mock_embed` fixture patches `kb_ingestion.embed_texts`.)

- [ ] **Step 4: Run + commit**

```bash
cd apps/api && python -m py_compile src/usan_api/compat/kb_ingestion_poller.py src/usan_api/main.py
uv run pytest tests/compat/test_kb_ingestion_poller.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/kb_ingestion_poller.py apps/api/src/usan_api/main.py apps/api/tests/compat/test_kb_ingestion_poller.py
git commit -m "feat(api): Phase 5 KB ingestion poller (cross-org claim + per-org processing) + lifespan"
```

---

### Task 7: kb_service (the 6 service functions)

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_service.py`
- Test: `apps/api/tests/compat/test_kb_service.py`

**Interfaces:**
- Consumes: Task 2 repo, Task 3 ids + `ParsedKbCreate`/`ParsedKbAddSources`.
- Produces: the 6 service fns (Interface contract). All commit; return ORM (router serializes). `content_url` minting lives here (a stable internal reference).

- [ ] **Step 1: Write the service** — validation → `CompatError`; `content_url` minted from the source id; commit on every mutation.

```python
"""KB compat service (Phase 5). Validation + persistence; returns ORM (router serializes).
Every mutation commits (get_compat_db does not autocommit). content_url is an internal
reference (content lives in the DB; not publicly served in v1 — documented posture)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.knowledge_bases import (
    KbTextInput,
    ParsedKbAddSources,
    ParsedKbCreate,
)
from usan_api.db.models import KnowledgeBase
from usan_api.repositories import knowledge_bases as repo

_NAME_MAX = 40
_CHUNK_MAX_LO, _CHUNK_MAX_HI = 600, 6000
_CHUNK_MIN_LO, _CHUNK_MIN_HI = 200, 2000


def _content_url(source_id: uuid.UUID) -> str:
    # Internal reference; not publicly served in v1 (documented posture).
    return f"https://knowledge-base.internal/source/{ids.encode_kb_source_id(source_id)}"


def _reject_unsupported_sources(has_files: bool, has_urls: bool) -> None:
    if has_files or has_urls:
        raise CompatError(422, "only text sources are supported")


def _validate_create(p: ParsedKbCreate) -> None:
    if not p.name or len(p.name) >= _NAME_MAX:
        raise CompatError(422, "invalid knowledge_base_name")
    if not (_CHUNK_MAX_LO <= p.max_chunk_size <= _CHUNK_MAX_HI):
        raise CompatError(422, "invalid max_chunk_size")
    if not (_CHUNK_MIN_LO <= p.min_chunk_size <= _CHUNK_MIN_HI):
        raise CompatError(422, "invalid min_chunk_size")
    if p.min_chunk_size >= p.max_chunk_size:
        raise CompatError(422, "min_chunk_size must be < max_chunk_size")
    _reject_unsupported_sources(p.has_files, p.has_urls)


async def _persist_texts(db: AsyncSession, kb_id: uuid.UUID, texts: list[KbTextInput]) -> None:
    for t in texts:
        src = await repo.add_source(
            db, kb_id, source_type="text", title=t.title, content=t.text, content_url=""
        )
        src.content_url = _content_url(src.id)
    await db.flush()


async def create_kb(db: AsyncSession, parsed: ParsedKbCreate) -> KnowledgeBase:
    _validate_create(parsed)
    kb = await repo.create_kb(
        db,
        name=parsed.name,
        max_chunk_size=parsed.max_chunk_size,
        min_chunk_size=parsed.min_chunk_size,
        enable_auto_refresh=parsed.enable_auto_refresh,
    )
    await _persist_texts(db, kb.id, parsed.texts)
    await db.commit()
    return kb


async def add_sources(
    db: AsyncSession, kb_id_token: str, parsed: ParsedKbAddSources
) -> KnowledgeBase:
    _reject_unsupported_sources(parsed.has_files, parsed.has_urls)
    kb_id = ids.decode_kb_id(kb_id_token)
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise CompatError(404, "knowledge base not found")
    await _persist_texts(db, kb_id, parsed.texts)
    await repo.mark_in_progress(db, kb_id)  # new sources are un-chunked -> re-claimed
    await db.commit()
    kb2 = await repo.get_kb(db, kb_id)
    assert kb2 is not None
    return kb2


async def get_kb(db: AsyncSession, kb_id_token: str) -> KnowledgeBase:
    kb = await repo.get_kb(db, ids.decode_kb_id(kb_id_token))
    if kb is None:
        raise CompatError(404, "knowledge base not found")
    return kb


async def list_kbs(db: AsyncSession) -> list[KnowledgeBase]:
    return await repo.list_kbs(db)


async def delete_kb(db: AsyncSession, kb_id_token: str) -> None:
    if not await repo.delete_kb(db, ids.decode_kb_id(kb_id_token)):
        raise CompatError(404, "knowledge base not found")
    await db.commit()


async def delete_source(
    db: AsyncSession, kb_id_token: str, source_id_token: str
) -> KnowledgeBase:
    kb_id = ids.decode_kb_id(kb_id_token)
    source_id = ids.decode_kb_source_id(source_id_token)
    kb = await repo.get_kb(db, kb_id)
    if kb is None or await repo.get_source(db, kb_id, source_id) is None:
        raise CompatError(404, "source not found")
    await repo.delete_source(db, source_id)
    await db.commit()
    kb2 = await repo.get_kb(db, kb_id)
    assert kb2 is not None
    return kb2
```

- [ ] **Step 2: Write the service test** `apps/api/tests/compat/test_kb_service.py` — uses `app_session` + `set_tenant_context`.

```python
import uuid

import pytest

from usan_api.compat import ids, kb_service
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.knowledge_bases import KbTextInput, ParsedKbAddSources, ParsedKbCreate
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


async def _ctx(app_session):
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)


@pytest.mark.asyncio
async def test_create_persists_sources_in_progress(app_session) -> None:
    await _ctx(app_session)
    parsed = ParsedKbCreate(name="kb", texts=[KbTextInput(title="t", text="body")])
    kb = await kb_service.create_kb(app_session, parsed)
    await _ctx(app_session)
    assert kb.status == "in_progress"
    srcs = await repo.get_sources(app_session, kb.id)
    assert len(srcs) == 1 and srcs[0].content_url.startswith(
        "https://knowledge-base.internal/source/source_"
    )


@pytest.mark.asyncio
async def test_create_rejects_files_and_bad_chunks(app_session) -> None:
    await _ctx(app_session)
    with pytest.raises(CompatError) as e1:
        await kb_service.create_kb(app_session, ParsedKbCreate(name="kb", has_files=True))
    assert e1.value.status_code == 422
    with pytest.raises(CompatError):
        await kb_service.create_kb(
            app_session, ParsedKbCreate(name="kb", min_chunk_size=2000, max_chunk_size=2000)
        )
    with pytest.raises(CompatError):
        await kb_service.create_kb(app_session, ParsedKbCreate(name="x" * 40))


@pytest.mark.asyncio
async def test_add_sources_resets_in_progress(app_session) -> None:
    await _ctx(app_session)
    kb = await kb_service.create_kb(app_session, ParsedKbCreate(name="kb"))
    await repo.set_status(app_session, kb.id, "complete")
    await app_session.commit()
    await _ctx(app_session)
    kb2 = await kb_service.add_sources(
        app_session,
        ids.encode_kb_id(kb.id),
        ParsedKbAddSources(texts=[KbTextInput(title="t", text="more")]),
    )
    assert kb2.status == "in_progress"


@pytest.mark.asyncio
async def test_delete_kb_404(app_session) -> None:
    await _ctx(app_session)
    with pytest.raises(CompatError) as e:
        await kb_service.delete_kb(app_session, ids.encode_kb_id(uuid.uuid4()))
    assert e.value.status_code == 404
```

- [ ] **Step 3: Run + commit**

```bash
cd apps/api && uv run pytest tests/compat/test_kb_service.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/kb_service.py apps/api/tests/compat/test_kb_service.py
git commit -m "feat(api): Phase 5 KB service (validation, content_url mint, commit-on-mutation)"
```

---

### Task 8: router (6 ops) + multipart + registration + 501 removal

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/knowledge_bases.py`
- Modify: `apps/api/src/usan_api/compat/app.py`, `apps/api/src/usan_api/compat/routers/unsupported.py`, `apps/api/tests/test_compat_fidelity.py`
- Test: `apps/api/tests/compat/test_kb_router.py`

**Interfaces:**
- Consumes: Task 7 service, Task 3 serializer + parsed DTOs.
- Produces: the 6 routes at the oracle paths/codes.

- [ ] **Step 1: Write the router** — multipart parse (spec §12: JSON-blob arrays via `Form`, files via `request.form().getlist`); exact paths/codes; `_audit`; `response_model_exclude_none=True`. The list route returns `list[KnowledgeBaseResponse]` (bare array).

```python
"""RetellAI knowledge-base compat routes (Phase 5). Exact paths + codes (201/201/200/200/
204/200). multipart create/add-sources: list fields arrive as JSON-string blobs (retell-sdk
_serialize_multipartform); files as 'knowledge_base_files[]'. response_model_exclude_none."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request, Response, status
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids, kb_service
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.kb_serializer import serialize_kb
from usan_api.compat.schemas.knowledge_bases import (
    KbTextInput,
    KnowledgeBaseResponse,
    ParsedKbAddSources,
    ParsedKbCreate,
)
from usan_api.repositories import knowledge_bases as repo

router = APIRouter(tags=["compat-knowledge-bases"])


def _audit(request: Request, op: str, kb_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, knowledge_base_id=kb_id).info("compat kb op={op}")


def _parse_texts(raw: str | None) -> list[KbTextInput]:
    if raw is None:
        return []
    try:
        items = json.loads(raw)
        return [KbTextInput.model_validate(i) for i in items]
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise CompatError(422, "invalid knowledge_base_texts") from exc


def _has_urls(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        return bool(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise CompatError(422, "invalid knowledge_base_urls") from exc


async def _has_files(request: Request) -> bool:
    form = await request.form()
    return bool(form.getlist("knowledge_base_files[]"))


@router.post(
    "/create-knowledge-base",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def create_knowledge_base(
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    knowledge_base_name: str = Form(...),
    knowledge_base_texts: str | None = Form(None),
    knowledge_base_urls: str | None = Form(None),
    enable_auto_refresh: bool = Form(False),
    max_chunk_size: int = Form(2000),
    min_chunk_size: int = Form(400),
) -> KnowledgeBaseResponse:
    parsed = ParsedKbCreate(
        name=knowledge_base_name,
        texts=_parse_texts(knowledge_base_texts),
        has_files=await _has_files(request),
        has_urls=_has_urls(knowledge_base_urls),
        enable_auto_refresh=enable_auto_refresh,
        max_chunk_size=max_chunk_size,
        min_chunk_size=min_chunk_size,
    )
    kb = await kb_service.create_kb(db, parsed)
    _audit(request, "create-knowledge-base", ids.encode_kb_id(kb.id))
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.post(
    "/add-knowledge-base-sources/{knowledge_base_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def add_knowledge_base_sources(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    knowledge_base_texts: str | None = Form(None),
    knowledge_base_urls: str | None = Form(None),
) -> KnowledgeBaseResponse:
    parsed = ParsedKbAddSources(
        texts=_parse_texts(knowledge_base_texts),
        has_files=await _has_files(request),
        has_urls=_has_urls(knowledge_base_urls),
    )
    kb = await kb_service.add_sources(db, knowledge_base_id, parsed)
    _audit(request, "add-knowledge-base-sources", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.get(
    "/get-knowledge-base/{knowledge_base_id}",
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def get_knowledge_base(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> KnowledgeBaseResponse:
    kb = await kb_service.get_kb(db, knowledge_base_id)
    _audit(request, "get-knowledge-base", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))


@router.get(
    "/list-knowledge-bases",
    response_model=list[KnowledgeBaseResponse],
    response_model_exclude_none=True,
)
async def list_knowledge_bases(
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> list[KnowledgeBaseResponse]:
    kbs = await kb_service.list_kbs(db)
    sources = await repo.get_sources_for_kbs(db, [k.id for k in kbs])
    _audit(request, "list-knowledge-bases")
    return [serialize_kb(k, sources.get(k.id, [])) for k in kbs]


@router.delete("/delete-knowledge-base/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await kb_service.delete_kb(db, knowledge_base_id)
    _audit(request, "delete-knowledge-base", knowledge_base_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/delete-knowledge-base-source/{knowledge_base_id}/source/{source_id}",
    response_model=KnowledgeBaseResponse,
    response_model_exclude_none=True,
)
async def delete_knowledge_base_source(
    knowledge_base_id: str,
    source_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> KnowledgeBaseResponse:
    kb = await kb_service.delete_source(db, knowledge_base_id, source_id)
    _audit(request, "delete-knowledge-base-source", knowledge_base_id)
    return serialize_kb(kb, await repo.get_sources(db, kb.id))
```

- [ ] **Step 2: Register** in `compat/app.py` — import `from usan_api.compat.routers import knowledge_bases as compat_knowledge_bases` (with the other router imports) and add `app.include_router(compat_knowledge_bases.router)` BEFORE `app.include_router(compat_unsupported.router)`.

- [ ] **Step 3: Remove the 501 stubs** — delete the 6 KB tuples (lines 39–44) from `compat/routers/unsupported.py` `_UNSUPPORTED`, and delete `("post", "/create-knowledge-base"),` (line 117) from `tests/test_compat_fidelity.py`.

- [ ] **Step 4: Write the router test** `apps/api/tests/compat/test_kb_router.py` — uses `compat_client` + `compat_headers`. POST multipart in the retell-sdk wire shape (`data={...}` with `knowledge_base_texts` a JSON string). Assert codes 201/201/200/200/204/200, auth-required, `status=="in_progress"` + sources absent on create, files-present→422.

```python
import json
import uuid


def _create(compat_client, headers, **over):
    data = {
        "knowledge_base_name": over.get("name", "Support"),
        "knowledge_base_texts": json.dumps(
            over.get("texts", [{"title": "FAQ", "text": "hello world"}])
        ),
    }
    data.update(over.get("extra", {}))
    return compat_client.post("/create-knowledge-base", data=data, headers=headers)


def test_create_in_progress_and_omits_sources(compat_client, compat_headers) -> None:
    r = _create(compat_client, compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["knowledge_base_id"].startswith("knowledge_base_")
    assert body["status"] == "in_progress"
    assert "knowledge_base_sources" not in body


def test_requires_key(compat_client) -> None:
    assert compat_client.get("/list-knowledge-bases").status_code == 401


def test_files_rejected_422(compat_client, compat_headers) -> None:
    files = {"knowledge_base_files[]": ("a.txt", b"data", "text/plain")}
    r = compat_client.post(
        "/create-knowledge-base",
        data={"knowledge_base_name": "K"},
        files=files,
        headers=compat_headers,
    )
    assert r.status_code == 422


def test_get_list_delete_lifecycle(compat_client, compat_headers) -> None:
    kid = _create(compat_client, compat_headers).json()["knowledge_base_id"]
    assert compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).status_code == 200
    assert compat_client.get("/list-knowledge-bases", headers=compat_headers).status_code == 200
    assert (
        compat_client.delete(f"/delete-knowledge-base/{kid}", headers=compat_headers).status_code
        == 204
    )
    assert compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).status_code == 404


def test_bad_id_422(compat_client, compat_headers) -> None:
    r = compat_client.get(f"/get-knowledge-base/agent_{uuid.uuid4().hex}", headers=compat_headers)
    assert r.status_code == 422
```

- [ ] **Step 5: Run (incl. the surface-coverage suites) + commit**

```bash
cd apps/api && python -m py_compile src/usan_api/compat/routers/knowledge_bases.py
uv run pytest tests/compat/test_kb_router.py tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/routers/knowledge_bases.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/test_compat_fidelity.py apps/api/tests/compat/test_kb_router.py
git commit -m "feat(api): Phase 5 KB router (6 ops, multipart, exact codes) + remove 501 stubs"
```

---

### Task 9: conformance freeze + surface verification

**Files:**
- Create: `apps/api/tests/compat/test_freeze_knowledge_bases.py`

**Interfaces:**
- Consumes: the served router (Task 8).

- [ ] **Step 1: Write the freeze test** (mirror `test_freeze_chat_analysis.py`): a `*_requires_key` 401 test + happy-path tests calling `assert_conforms("KnowledgeBaseResponse")` + `assert_sdk_roundtrip("retell.types:KnowledgeBaseResponse")` for create (in_progress), get, and list (validate each element).

```python
import json

from .conformance import assert_conforms, assert_sdk_roundtrip


def _create(compat_client, headers):
    return compat_client.post(
        "/create-knowledge-base",
        data={
            "knowledge_base_name": "Support",
            "knowledge_base_texts": json.dumps([{"title": "FAQ", "text": "hi"}]),
        },
        headers=headers,
    )


def test_create_requires_key(compat_client) -> None:
    assert compat_client.post("/create-knowledge-base", data={}).status_code == 401


def test_create_conforms(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers).json()
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_get_conforms(compat_client, compat_headers) -> None:
    kid = _create(compat_client, compat_headers).json()["knowledge_base_id"]
    body = compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).json()
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_list_each_element_conforms(compat_client, compat_headers) -> None:
    _create(compat_client, compat_headers)
    arr = compat_client.get("/list-knowledge-bases", headers=compat_headers).json()
    assert isinstance(arr, list) and arr
    for item in arr:
        assert_conforms(item, "KnowledgeBaseResponse")
        assert_sdk_roundtrip(item, "retell.types:KnowledgeBaseResponse")
```

- [ ] **Step 2: Run + commit**

```bash
cd apps/api && uv run pytest tests/compat/test_freeze_knowledge_bases.py tests/compat/test_surface_coverage.py -v
ruff check . && ruff format . && uv run mypy
git add apps/api/tests/compat/test_freeze_knowledge_bases.py
git commit -m "test(api): Phase 5 KB conformance freeze (assert_conforms + sdk_roundtrip)"
```

---

### Task 10: deploy doc + env passthrough

**Files:**
- Create: `docs/deployment/knowledge-bases.md`
- Modify: `infra/docker-compose.yml` (api `environment:` map), `infra/.env.prod.example`

**Interfaces:** none (docs/config).

- [ ] **Step 1: Write `docs/deployment/knowledge-bases.md`** — document: the 6 served ops; the inert defaults; the activation order (set `KB_EMBEDDING_ENABLED=true`, `GCP_PROJECT`, `KB_INGESTION_POLLER_ENABLED=true` in Secret Manager `usan-prod-env` + VM `.env` BEFORE the `v*` tag); the **VERIFY-at-deploy** items (the `usan` owner's `CREATE EXTENSION vector` privilege on Cloud SQL; `text-embedding-005` returns 768-dim from `us-central1`); and the posture deviations (text-only / files+urls→422; `content_url` internal; `enable_auto_refresh` no-op; `knowledge_base_ids` echo-only; `refreshing_in_progress` never emitted).

- [ ] **Step 2: Env passthrough** — add the 7 `KB_*` keys to the api service `environment:` map in `infra/docker-compose.yml` (using the `${KB_...:-default}` form the other compat/chat keys use) and to `infra/.env.prod.example` (all OFF / defaults). Mirror how `CHAT_ANALYSIS_*` is wired (grep `CHAT_ANALYSIS_ENABLED` in both files).

- [ ] **Step 3: Verify compose + commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && docker compose -f infra/docker-compose.yml config >/dev/null && echo "compose OK"
git add docs/deployment/knowledge-bases.md infra/docker-compose.yml infra/.env.prod.example
git commit -m "docs(infra): Phase 5 KB deployment doc + compose/.env passthrough (inert)"
```

---

## Final whole-branch review

After Task 10, run the SDD final whole-branch review (most-capable model) over `git merge-base main HEAD..HEAD`, then the independent `/review`. Full apps/api gate must be green: `ruff check . && ruff format . && uv run mypy && uv run pytest`, single alembic head `0047`. Then `finishing-a-development-branch` → push + open PR (squash-merge on explicit go-ahead, **no `v*` tag**).

## Self-review notes (author)
- **Spec coverage:** all 6 ops (T8), 3 tables + extension + HNSW + claim fn (T1), repos (T2), ids+schemas+serializer (T3), chunking+embed+settings (T4), ingestion (T5), cross-org poller (T6), service (T7), conformance (T9), docs/env (T10). ✔
- **Type consistency:** `serialize_kb(kb, sources)` identical T3/T8; `claim_pending(db, *, limit, lease_seconds)` T2↔T6; `ingest_one_kb(db, kb_id, settings)` T5↔T6; `ParsedKbCreate/ParsedKbAddSources` T3↔T7↔T8; `create_kb(db, parsed)` T7↔T8. ✔
- **Known implementer adjustments (flagged inline, not placeholders):** (1) the `organizations` seed columns in the cross-org tests must match the real `Organization` model — prefer an existing second-org factory if present; (2) confirm `app_async_database_url`/`app_role_password` fixture names + that the poller test factory connects as `usan_app`; (3) `await request.form()` after `Form(...)` params is safe (Starlette caches the parsed form) — `test_files_rejected_422` is the guard; (4) the `mock_embed` fixture patches `usan_api.compat.kb_ingestion.embed_texts` (the call site), reused by the poller test.
