# RetellAI Parity Phase 4c-2 — rerun-chat-analysis + Vertex chat_analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `PUT /rerun-chat-analysis/{chat_id}` (201 `ChatResponse`), backed by a Vertex post-chat analysis pipeline that mirrors `summarization.py`, persisting to a new `chat_analyses` table and surfacing `chat_analysis` on the shared chat serializer.

**Architecture:** A new TenantScoped+RLS `chat_analyses` table (one row per chat, `chat_session_id` UNIQUE → upsert) holds the analysis. A native `chat_analysis.py` pipeline runs one Vertex turn over the chat transcript, parses `{chat_summary, user_sentiment, chat_successful}`, and upserts. The rerun op runs the pipeline inline (`force=True`) and returns the chat; `chat_analysis` is added to `CompatChat` so get/list-chats reflect stored analysis (single load + batched load → no N+1).

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, google-genai (Vertex via ADC), pytest, the compat oracle conformance harness.

## Global Constraints

- `apps/api` and `services/agent` never import each other. `chat_analysis.py` is a native `usan_api` module; it must not import `services/agent`.
- `organization_id` is server-set by the DB default + RLS; app code (repos, pipeline) NEVER assigns it.
- PHI/secret-safe logging only: `_audit` logs org id + op + chat_id ONLY; pipeline errors log `type(exc).__name__` ONLY — never transcript/summary/sentiment/prompt/numbers.
- `exclude_none` discipline: every serialized response omits None keys (a serialized `null` fails the pinned oracle). `CompatChat` uses `| None = None`; routes set `response_model_exclude_none=True`.
- `user_sentiment` must be exactly one of `Negative | Positive | Neutral | Unknown` or absent — coerced ONCE at write time in the pipeline; the serializer is a straight pass-through (no re-coercion).
- Oracle exact path string: `/rerun-chat-analysis/{chat_id}` (PUT, 201). `KNOWN_GAPS` stays `frozenset()`.
- Migration is `0046`, `down_revision="0045"`, owner-DDL, additive, **inert** (no `v*` tag this phase).
- CI runs `ruff check . && ruff format .`, `uv run mypy` (config `files=["src"]` — NEVER `mypy .`), and the full `uv run pytest` (`-n auto`). Commit scope `api`. Attribution disabled (no `Co-Authored-By`, no 🤖 footer).
- The display of `except (A, B):` may render without parens in this environment — the REAL code keeps the parens; verify via `python -m py_compile`, not by eye.

**Working dir for all commands:** `apps/api`. Run tests serially with `uv run pytest -n0 <path> -q` during TDD (the suite default is `-n auto`).

---

### Task 1: Migration 0046 + ORM `ChatAnalysisRecord`

**Files:**
- Create: `apps/api/migrations/versions/0046_chat_analyses.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add `ChatAnalysisRecord` after `ChatMessage`, ends line 1348)
- Test: `apps/api/tests/test_chat_analyses_table.py`

**Interfaces:**
- Produces: ORM `usan_api.db.models.ChatAnalysisRecord` (table `chat_analyses`) with columns `id, organization_id, chat_session_id (UNIQUE FK→chat_sessions ON DELETE CASCADE), chat_summary: str|None, user_sentiment: str|None, chat_successful: bool|None, custom_analysis_data: dict|None, model_version: str, created_at, updated_at`. Alembic head becomes `0046`.

- [ ] **Step 1: Write the failing structural test**

Create `apps/api/tests/test_chat_analyses_table.py`:

```python
"""Phase 4c-2: the 0046 migration ships chat_analyses (columns + RLS + unique)."""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_chat_analyses_columns(app_session) -> None:
    rows = (
        await app_session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_name = 'chat_analyses'"
            )
        )
    ).all()
    cols = {r.column_name: (r.data_type, r.is_nullable) for r in rows}
    assert cols["organization_id"][0] == "uuid"
    assert cols["chat_session_id"][0] == "uuid"
    assert cols["chat_summary"] == ("text", "YES")
    assert cols["user_sentiment"] == ("text", "YES")
    assert cols["chat_successful"] == ("boolean", "YES")
    assert cols["custom_analysis_data"][0] == "jsonb"
    assert cols["model_version"][0] == "text"


@pytest.mark.asyncio
async def test_chat_analyses_rls_forced(app_session) -> None:
    row = (
        await app_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = 'chat_analyses'"
            )
        )
    ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


@pytest.mark.asyncio
async def test_chat_analyses_session_id_unique(app_session) -> None:
    cons = (
        await app_session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'chat_analyses'::regclass "
                "AND contype = 'u'"
            )
        )
    ).scalars().all()
    assert any("session" in c for c in cons)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/test_chat_analyses_table.py -q`
Expected: FAIL — `chat_analyses` table does not exist (the migration hasn't run).

- [ ] **Step 3: Write the migration**

Create `apps/api/migrations/versions/0046_chat_analyses.py`:

```python
"""chat_analyses: TenantScoped + FORCE RLS table for post-chat analysis (Phase 4c-2).

New owner-DDL table (modeled on 0042). One row per chat (chat_session_id UNIQUE → the
rerun-chat-analysis op upserts in place). GRANT to usan_app so the least-priv runtime role
can CRUD it. Additive + inert until a v* tag.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
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


def upgrade() -> None:
    op.create_table(
        "chat_analyses",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("chat_session_id", sa.Uuid(), nullable=False),
        sa.Column("chat_summary", sa.Text(), nullable=True),
        sa.Column("user_sentiment", sa.Text(), nullable=True),
        sa.Column("chat_successful", sa.Boolean(), nullable=True),
        sa.Column("custom_analysis_data", postgresql.JSONB(), nullable=True),
        sa.Column("model_version", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_session_id", name="uq_chat_analyses_session"),
    )
    op.create_index("ix_chat_analyses_organization_id", "chat_analyses", ["organization_id"])
    _enable_rls("chat_analyses")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_analyses")
    op.drop_index("ix_chat_analyses_organization_id", table_name="chat_analyses")
    op.drop_table("chat_analyses")
```

- [ ] **Step 4: Add the ORM model**

In `apps/api/src/usan_api/db/models.py`, immediately after the `ChatMessage` class (which ends at line 1348), add:

```python
class ChatAnalysisRecord(Base, TenantScoped):
    """Post-chat analysis for one chat session (Phase 4c-2).

    One row per chat (``chat_session_id`` is UNIQUE → the rerun-chat-analysis op upserts in
    place). Mirrors ``ConversationSummary`` for the chat channel. Vertex-generated; the
    summary text is PHI and stays on BAA Postgres. ``model_version`` records the analyzing
    model for audit.
    """

    __tablename__ = "chat_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    chat_summary: Mapped[str | None] = mapped_column(Text)
    user_sentiment: Mapped[str | None] = mapped_column(Text)
    chat_successful: Mapped[bool | None] = mapped_column(Boolean)
    custom_analysis_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

(`Boolean`, `Text`, `ForeignKey`, `DateTime`, `func`, `text`, `UUID`, `JSONB`, `Any` are already imported in this module — verified.)

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest -n0 tests/test_chat_analyses_table.py -q`
Expected: PASS (the testcontainer applies migrations through 0046).

- [ ] **Step 6: Confirm a single alembic head + lint**

Run: `uv run alembic heads` → Expected: `0046 (head)` (one head).
Run: `ruff check . && ruff format --check . && uv run mypy`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/migrations/versions/0046_chat_analyses.py apps/api/src/usan_api/db/models.py apps/api/tests/test_chat_analyses_table.py
git commit -m "feat(api): chat_analyses table + ChatAnalysisRecord ORM (Phase 4c-2)"
```

---

### Task 2: `repositories/chat_analyses.py`

**Files:**
- Create: `apps/api/src/usan_api/repositories/chat_analyses.py`
- Test: `apps/api/tests/test_chat_analyses_repo.py`

**Interfaces:**
- Consumes: `ChatAnalysisRecord` (Task 1).
- Produces:
  - `get_for_session(db, session_id: uuid.UUID) -> ChatAnalysisRecord | None`
  - `get_for_sessions(db, session_ids: list[uuid.UUID]) -> dict[uuid.UUID, ChatAnalysisRecord]`
  - `upsert(db, session_id: uuid.UUID, *, chat_summary: str | None, user_sentiment: str | None, chat_successful: bool | None, custom_analysis_data: dict[str, Any] | None, model_version: str) -> ChatAnalysisRecord`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_chat_analyses_repo.py`:

```python
"""Phase 4c-2: chat_analyses repo — upsert overwrite, batched load, cross-org RLS."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as repo
from usan_api.repositories import chats as chats_repo
from usan_api.tenant_context import set_tenant_context


async def _seed_session(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    return session.id


@pytest.mark.asyncio
async def test_upsert_inserts_then_overwrites(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session(app_session)

    first = await repo.upsert(
        app_session,
        sid,
        chat_summary="first",
        user_sentiment="Neutral",
        chat_successful=False,
        custom_analysis_data=None,
        model_version="m1",
    )
    assert first.chat_summary == "first"

    second = await repo.upsert(
        app_session,
        sid,
        chat_summary="second",
        user_sentiment="Positive",
        chat_successful=True,
        custom_analysis_data=None,
        model_version="m2",
    )
    assert second.chat_summary == "second"
    assert second.user_sentiment == "Positive"
    assert second.chat_successful is True

    # Exactly one row for the session (upsert overwrote, did not duplicate).
    count = (
        await app_session.execute(
            text("SELECT count(*) FROM chat_analyses WHERE chat_session_id = :s"), {"s": sid}
        )
    ).scalar_one()
    assert count == 1
    await app_session.rollback()


@pytest.mark.asyncio
async def test_get_for_sessions_batched(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid1 = await _seed_session(app_session)
    sid2 = await _seed_session(app_session)
    await repo.upsert(
        app_session, sid1, chat_summary="a", user_sentiment=None,
        chat_successful=None, custom_analysis_data=None, model_version="m",
    )
    # sid2 has no analysis.
    got = await repo.get_for_sessions(app_session, [sid1, sid2])
    assert set(got.keys()) == {sid1}
    assert got[sid1].chat_summary == "a"
    assert await repo.get_for_sessions(app_session, []) == {}
    await app_session.rollback()


@pytest.mark.asyncio
async def test_cross_org_isolation(app_session, two_orgs) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    sid = await _seed_session(app_session)
    await repo.upsert(
        app_session, sid, chat_summary="secret", user_sentiment=None,
        chat_successful=None, custom_analysis_data=None, model_version="m",
    )
    assert await repo.get_for_session(app_session, sid) is not None

    # Switching the RLS context to org B hides org A's analysis row.
    await set_tenant_context(app_session, org_b)
    assert await repo.get_for_session(app_session, sid) is None
    await app_session.rollback()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/test_chat_analyses_repo.py -q`
Expected: FAIL — `usan_api.repositories.chat_analyses` does not exist.

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/chat_analyses.py`:

```python
"""chat_analyses repository (Phase 4c-2). Post-chat analysis; RLS-scoped, org auto-filled.

``upsert`` overwrites in place (the rerun op recomputes), keyed on the unique
``chat_session_id``. ``get_for_sessions`` batches the list path (one IN query → no N+1).
Flush-only; the caller commits. Mirrors ``conversation_summaries`` for the chat channel.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ChatAnalysisRecord


async def get_for_session(
    db: AsyncSession, session_id: uuid.UUID
) -> ChatAnalysisRecord | None:
    stmt = select(ChatAnalysisRecord).where(ChatAnalysisRecord.chat_session_id == session_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_for_sessions(
    db: AsyncSession, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ChatAnalysisRecord]:
    if not session_ids:
        return {}
    stmt = select(ChatAnalysisRecord).where(
        ChatAnalysisRecord.chat_session_id.in_(session_ids)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {r.chat_session_id: r for r in rows}


async def upsert(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    chat_summary: str | None,
    user_sentiment: str | None,
    chat_successful: bool | None,
    custom_analysis_data: dict[str, Any] | None,
    model_version: str,
) -> ChatAnalysisRecord:
    """Insert or overwrite the analysis for ``session_id`` (the rerun op recomputes).

    ON CONFLICT (chat_session_id) DO UPDATE keeps one row per chat. organization_id is
    never set here — the DB default fills it and RLS WITH CHECK enforces the tenant.
    """
    values = {
        "chat_summary": chat_summary,
        "user_sentiment": user_sentiment,
        "chat_successful": chat_successful,
        "custom_analysis_data": custom_analysis_data,
        "model_version": model_version,
    }
    stmt = (
        pg_insert(ChatAnalysisRecord)
        .values(chat_session_id=session_id, **values)
        .on_conflict_do_update(
            index_elements=[ChatAnalysisRecord.chat_session_id],
            set_={**values, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
    await db.flush()
    record = await get_for_session(db, session_id)
    assert record is not None  # just upserted
    return record
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest -n0 tests/test_chat_analyses_repo.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

Run: `ruff check . && ruff format --check . && uv run mypy`
```bash
git add apps/api/src/usan_api/repositories/chat_analyses.py apps/api/tests/test_chat_analyses_repo.py
git commit -m "feat(api): chat_analyses repository (get/get_for_sessions/upsert) (Phase 4c-2)"
```

---

### Task 3: Settings keys + `chat_analysis.py` Vertex pipeline

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (add two fields after line 241)
- Create: `apps/api/src/usan_api/chat_analysis.py`
- Test: `apps/api/tests/test_chat_analysis_pipeline.py`

**Interfaces:**
- Consumes: `chat_analyses_repo` (Task 2), `chats_repo.list_messages`, `run_vertex_turn`, `Settings`.
- Produces: `analyze_chat_with(db, session_id: uuid.UUID, settings: Settings, *, force: bool = False) -> ChatAnalysisRecord | None`. Settings gain `chat_analysis_enabled: bool` and `chat_analysis_model: str`.

- [ ] **Step 1: Add the settings fields**

In `apps/api/src/usan_api/settings.py`, immediately after line 241 (`summarization_model = ...`), add:

```python
    # Post-chat analysis (Phase 4c-2 / rerun-chat-analysis). Ship-inert: default OFF, so no
    # Vertex call (spend or PHI egress) until a deploy enables it AND gcp_project is set.
    # Reuses the Vertex ADC path (Constitution II) — never the Gemini Developer API.
    chat_analysis_enabled: bool = Field(default=False, alias="CHAT_ANALYSIS_ENABLED")
    chat_analysis_model: str = Field(default="gemini-2.5-flash", alias="CHAT_ANALYSIS_MODEL")
```

- [ ] **Step 2: Write the failing pipeline test**

Create `apps/api/tests/test_chat_analysis_pipeline.py`:

```python
"""Phase 4c-2: analyze_chat_with — gate, force/idempotent, parse, coercion, error swallow."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from usan_api import chat_analysis
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn


def _settings(*, enabled: bool = True, project: str | None = "p"):
    return get_settings().model_copy(
        update={"chat_analysis_enabled": enabled, "gcp_project": project}
    )


async def _seed_session_with_message(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    await chats_repo.add_message(
        db, session_id=session.id, seq=1, role="user", content="I am doing great today"
    )
    await db.flush()
    return session.id


def _patch_vertex(monkeypatch, payload: str) -> None:
    async def _fake(**kwargs):
        assert kwargs["tools"] == []
        return VertexTurn(text=payload)

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _fake)


@pytest.mark.asyncio
async def test_analyze_persists_parsed_fields(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(
        monkeypatch,
        json.dumps(
            {"chat_summary": "A warm check-in.", "user_sentiment": "Positive", "chat_successful": True}
        ),
    )
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.chat_summary == "A warm check-in."
    assert rec.user_sentiment == "Positive"
    assert rec.chat_successful is True
    await app_session.rollback()


@pytest.mark.asyncio
async def test_sentiment_off_enum_coerced_to_none(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(
        monkeypatch,
        json.dumps({"chat_summary": "x", "user_sentiment": "ecstatic", "chat_successful": "yes"}),
    )
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None
    assert rec.user_sentiment is None  # off-enum dropped
    assert rec.chat_successful is None  # non-bool dropped
    await app_session.rollback()


@pytest.mark.asyncio
async def test_lowercase_sentiment_normalized(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(monkeypatch, json.dumps({"chat_summary": "x", "user_sentiment": "negative"}))
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None and rec.user_sentiment == "Negative"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_non_json_degrades_to_summary(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    _patch_vertex(monkeypatch, "the user was happy")  # not JSON
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None and rec.chat_summary == "the user was happy"
    assert rec.user_sentiment is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_flag_off_is_noop(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return VertexTurn(text="{}")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(
        app_session, sid, _settings(enabled=False), force=True
    )
    assert rec is None  # no prior record, no Vertex
    assert called is False
    await app_session.rollback()


@pytest.mark.asyncio
async def test_idempotent_without_force(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    await analyses_repo.upsert(
        app_session, sid, chat_summary="prior", user_sentiment=None,
        chat_successful=None, custom_analysis_data=None, model_version="m",
    )

    async def _boom(**kwargs):
        raise AssertionError("Vertex must not be called when a record exists and force=False")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=False)
    assert rec is not None and rec.chat_summary == "prior"
    await app_session.rollback()


@pytest.mark.asyncio
async def test_zero_messages_noop(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hi"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    app_session.add(profile)
    await app_session.flush()
    session = await chats_repo.add_session(
        app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await app_session.flush()

    async def _boom(**kwargs):
        raise AssertionError("Vertex must not be called for a zero-message chat")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _boom)
    rec = await chat_analysis.analyze_chat_with(app_session, session.id, _settings(), force=True)
    assert rec is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_vertex_error_swallowed_returns_prior(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session_with_message(app_session)
    prior = await analyses_repo.upsert(
        app_session, sid, chat_summary="prior", user_sentiment=None,
        chat_successful=None, custom_analysis_data=None, model_version="m",
    )

    async def _raise(**kwargs):
        raise RuntimeError("vertex down")

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _raise)
    rec = await chat_analysis.analyze_chat_with(app_session, sid, _settings(), force=True)
    assert rec is not None and rec.id == prior.id  # error swallowed → prior record returned
    await app_session.rollback()
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/test_chat_analysis_pipeline.py -q`
Expected: FAIL — `usan_api.chat_analysis` does not exist.

- [ ] **Step 4: Write the pipeline**

Create `apps/api/src/usan_api/chat_analysis.py`:

```python
"""Post-chat analysis pipeline (Phase 4c-2 / rerun-chat-analysis).

One Vertex turn over the chat transcript produces ``chat_summary`` + ``user_sentiment`` +
``chat_successful`` (the oracle ChatAnalysis fields). Mirrors ``summarization.py`` for the
chat channel: Vertex via ``vertexai=True`` + ADC ONLY (Constitution II PHI containment),
defensive JSON parse, sentiment coerced to the closed enum, and a try/except that logs the
exception TYPE only and never raises — so the inline rerun endpoint always returns 201.

``analyze_chat_with`` is the reusable core: gated on ``chat_analysis_enabled`` + a configured
``gcp_project`` (ship-inert), idempotent without ``force`` (a future auto-trigger), and a
no-op for an empty chat. ``custom_analysis_data`` is deferred (always ``None`` this phase).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ChatAnalysisRecord, ChatMessage
from usan_api.repositories import chat_analyses as chat_analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn

_MAX_TRANSCRIPT_CHARS = 12000
_MAX_SUMMARY_CHARS = 4000

# The oracle ChatAnalysis.user_sentiment enum (title-case). Anything else -> None.
_VALID_SENTIMENTS: frozenset[str] = frozenset({"Negative", "Positive", "Neutral", "Unknown"})

_SYSTEM_INSTRUCTION = (
    "You analyze a chat conversation between an assistant agent and a user. "
    "Respond with ONLY a JSON object, no markdown, with keys: "
    '"chat_summary" (a 1-3 sentence high-level recap, warm and factual), '
    '"user_sentiment" (exactly one of: Positive, Negative, Neutral, Unknown), and '
    '"chat_successful" (a boolean: whether the agent seems to have accomplished the '
    "user's goal in the chat). Do not invent details."
)


@dataclass(frozen=True)
class _ParsedAnalysis:
    chat_summary: str | None = None
    user_sentiment: str | None = None
    chat_successful: bool | None = None


def _render_transcript(messages: list[ChatMessage]) -> str:
    lines = [f"{m.role}: {m.content}" for m in messages if m.content and m.content.strip()]
    return "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ```json fence some models add. Kept local so this pipeline
    has no dependency on the parallel summarization module."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _coerce_sentiment(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    norm = raw.strip().capitalize()  # "POSITIVE"/"positive" -> "Positive"
    return norm if norm in _VALID_SENTIMENTS else None


def _parse_analysis(text: str) -> _ParsedAnalysis:
    """Parse the model's JSON defensively; a non-JSON reply degrades to a raw-text summary."""
    raw_text = (text or "").strip()
    try:
        data = json.loads(_strip_code_fence(raw_text))
    except (json.JSONDecodeError, ValueError):
        return _ParsedAnalysis(chat_summary=raw_text[:_MAX_SUMMARY_CHARS] or None)
    if not isinstance(data, dict):
        return _ParsedAnalysis(chat_summary=raw_text[:_MAX_SUMMARY_CHARS] or None)
    summary_raw = data.get("chat_summary")
    summary = (
        str(summary_raw).strip()[:_MAX_SUMMARY_CHARS]
        if isinstance(summary_raw, str) and summary_raw.strip()
        else None
    )
    successful_raw = data.get("chat_successful")
    return _ParsedAnalysis(
        chat_summary=summary,
        user_sentiment=_coerce_sentiment(data.get("user_sentiment")),
        chat_successful=successful_raw if isinstance(successful_raw, bool) else None,
    )


async def analyze_chat_with(
    db: AsyncSession,
    session_id: uuid.UUID,
    settings: Settings,
    *,
    force: bool = False,
) -> ChatAnalysisRecord | None:
    """Analyze one chat and upsert the result. Returns the analysis row, the prior row, or
    None. Flush-only (the caller commits). Never raises — a Vertex/parse failure returns the
    prior record so the inline rerun endpoint still answers 201."""
    existing = await chat_analyses_repo.get_for_session(db, session_id)
    if not (settings.chat_analysis_enabled and settings.gcp_project):
        return existing  # ship-inert / unconfigured -> no PHI leaves, no spend
    if existing is not None and not force:
        return existing  # idempotent: already analyzed (a future auto-trigger path)
    messages = await chats_repo.list_messages(db, session_id)
    if not messages:
        return existing  # nothing to analyze -> no Vertex call
    transcript = _render_transcript(messages)
    try:
        turn = await run_vertex_turn(
            model=settings.chat_analysis_model,
            temperature=0.2,
            system_instruction=_SYSTEM_INSTRUCTION,
            tools=[],
            contents=[{"role": "user", "parts": [{"text": transcript}]}],
            settings=settings,
        )
        parsed = _parse_analysis(turn.text)
        return await chat_analyses_repo.upsert(
            db,
            session_id,
            chat_summary=parsed.chat_summary,
            user_sentiment=parsed.user_sentiment,
            chat_successful=parsed.chat_successful,
            custom_analysis_data=None,
            model_version=settings.chat_analysis_model,
        )
    except Exception as exc:  # noqa: BLE001 - never break the inline endpoint; log TYPE only
        logger.bind(err=type(exc).__name__).error("chat analysis crashed")
        return existing
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest -n0 tests/test_chat_analysis_pipeline.py -q`
Expected: PASS (8 tests).

- [ ] **Step 6: Verify the `except (…)` parens survived + lint**

Run: `python -m py_compile src/usan_api/chat_analysis.py` → Expected: no output (valid syntax; the `except (json.JSONDecodeError, ValueError):` tuple is intact).
Run: `ruff check . && ruff format --check . && uv run mypy`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/settings.py apps/api/src/usan_api/chat_analysis.py apps/api/tests/test_chat_analysis_pipeline.py
git commit -m "feat(api): Vertex chat_analysis pipeline + CHAT_ANALYSIS_* settings (Phase 4c-2)"
```

---

### Task 4: Compat schema `ChatAnalysis` + `CompatChat.chat_analysis` + serializer pass-through

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/chats.py` (add `ChatAnalysis`; add field to `CompatChat`)
- Modify: `apps/api/src/usan_api/compat/chat_serializer.py` (add `analysis=` param)
- Test: `apps/api/tests/compat/test_chat_analysis_serializer.py`

**Interfaces:**
- Consumes: `ChatAnalysisRecord` (Task 1).
- Produces: `usan_api.compat.schemas.chats.ChatAnalysis` (4 optional fields); `CompatChat.chat_analysis: ChatAnalysis | None = None`; `serialize_chat(session, messages, *, include_transcript, analysis: ChatAnalysisRecord | None = None) -> CompatChat`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_chat_analysis_serializer.py`:

```python
"""Phase 4c-2: serialize_chat emits chat_analysis only when a record is passed (pass-through)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from usan_api.compat.chat_serializer import serialize_chat
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatAnalysisRecord, ChatSession


def _session() -> ChatSession:
    s = ChatSession(
        id=uuid.uuid4(),
        agent_profile_id=uuid.uuid4(),
        agent_version=1,
        status=ChatStatus.ENDED,
        chat_type="api_chat",
        dynamic_vars={},
    )
    s.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    s.ended_at = datetime(2026, 1, 1, tzinfo=UTC)
    return s


def test_no_analysis_omits_field():
    out = serialize_chat(_session(), [], include_transcript=False).model_dump(exclude_none=True)
    assert "chat_analysis" not in out


def test_analysis_passed_through():
    rec = ChatAnalysisRecord(
        chat_session_id=uuid.uuid4(),
        chat_summary="A warm check-in.",
        user_sentiment="Positive",
        chat_successful=True,
        custom_analysis_data=None,
    )
    out = serialize_chat(
        _session(), [], include_transcript=False, analysis=rec
    ).model_dump(exclude_none=True)
    assert out["chat_analysis"]["chat_summary"] == "A warm check-in."
    assert out["chat_analysis"]["user_sentiment"] == "Positive"
    assert out["chat_analysis"]["chat_successful"] is True
    assert "custom_analysis_data" not in out["chat_analysis"]  # None -> omitted
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/compat/test_chat_analysis_serializer.py -q`
Expected: FAIL — `serialize_chat()` got an unexpected keyword argument `analysis` (and `ChatAnalysisRecord` import / `chat_analysis` key missing).

- [ ] **Step 3: Add the `ChatAnalysis` schema + `CompatChat` field**

In `apps/api/src/usan_api/compat/schemas/chats.py`, add a `ChatAnalysis` class immediately before `class CompatChat` (line 87), and add the field to `CompatChat`:

```python
class ChatAnalysis(BaseModel):
    """Oracle ChatAnalysis sub-object. All optional; the serializer omits None via exclude_none."""

    chat_summary: str | None = None
    user_sentiment: str | None = None
    chat_successful: bool | None = None
    custom_analysis_data: dict[str, Any] | None = None


class CompatChat(BaseModel):
    chat_id: str
    agent_id: str
    chat_status: str
    version: int | None = None
    chat_type: str = "api_chat"
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    start_timestamp: int | None = None
    end_timestamp: int | None = None
    transcript: str | None = None
    message_with_tool_calls: list[CompatChatMessage] | None = None
    chat_analysis: ChatAnalysis | None = None
```

(`Any` and `BaseModel` are already imported in this module.)

- [ ] **Step 4: Add the `analysis=` param to the serializer**

In `apps/api/src/usan_api/compat/chat_serializer.py`, update the imports and `serialize_chat`:

```python
from usan_api.compat.schemas.chats import ChatAnalysis, CompatChat, CompatChatMessage
from usan_api.db.models import ChatAnalysisRecord, ChatMessage, ChatSession
```

```python
def serialize_chat(
    session: ChatSession,
    messages: list[ChatMessage],
    *,
    include_transcript: bool,
    analysis: ChatAnalysisRecord | None = None,
) -> CompatChat:
    """Build the RetellAI ChatResponse. include_transcript=False on the list path so
    transcript + message_with_tool_calls are omitted (V3ChatResponse forbids those keys).
    chat_analysis is a straight pass-through of the (already enum-coerced) stored record."""
    bare_vars, metadata = unpack_dynamic_vars(session.dynamic_vars)

    transcript: str | None = None
    message_with_tool_calls: list[CompatChatMessage] | None = None
    if include_transcript:
        transcript = "\n".join(_line(m) for m in messages)
        message_with_tool_calls = [
            CompatChatMessage(
                role=m.role,
                content=m.content,
                message_id=ids.encode_message_id(m.id),
                created_timestamp=to_ms(m.created_at) or 0,
            )
            for m in messages
        ]

    chat_analysis: ChatAnalysis | None = None
    if analysis is not None:
        chat_analysis = ChatAnalysis(
            chat_summary=analysis.chat_summary,
            user_sentiment=analysis.user_sentiment,
            chat_successful=analysis.chat_successful,
            custom_analysis_data=analysis.custom_analysis_data,
        )

    return CompatChat(
        chat_id=ids.encode_chat_id(session.id),
        agent_id=ids.encode_agent_id(session.agent_profile_id),
        chat_status=session.status.value,
        version=session.agent_version,
        chat_type=session.chat_type,
        retell_llm_dynamic_variables=bare_vars or None,
        metadata=metadata or None,
        start_timestamp=to_ms(session.started_at),
        end_timestamp=to_ms(session.ended_at),
        transcript=transcript,
        message_with_tool_calls=message_with_tool_calls,
        chat_analysis=chat_analysis,
    )
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest -n0 tests/compat/test_chat_analysis_serializer.py -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Confirm the existing chat serializer tests still pass + lint**

Run: `uv run pytest -n0 tests/compat/test_chat_repo_serializer.py -q`
Expected: PASS (unchanged — `analysis` defaults to None so `chat_analysis` is omitted).
Run: `ruff check . && ruff format --check . && uv run mypy`

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/chats.py apps/api/src/usan_api/compat/chat_serializer.py apps/api/tests/compat/test_chat_analysis_serializer.py
git commit -m "feat(api): ChatAnalysis schema + chat_analysis on CompatChat serializer (Phase 4c-2)"
```

---

### Task 5: `chat_service.rerun_chat_analysis`

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (add `rerun_chat_analysis`; import `chat_analysis`)
- Test: `apps/api/tests/compat/test_rerun_chat_service.py`

**Interfaces:**
- Consumes: `chats_repo.get_session`, `ids.decode_chat_id`, `chat_analysis.analyze_chat_with` (Task 3), `CompatError`.
- Produces: `rerun_chat_analysis(db, settings: Settings, chat_id: str) -> ChatSession` — loads the session (404 if missing/archived), runs `analyze_chat_with(force=True)`, commits, returns the session.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_rerun_chat_service.py`:

```python
"""Phase 4c-2: chat_service.rerun_chat_analysis — 404, and upserts analysis under force."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from usan_api.compat import chat_service, ids
from usan_api.compat.errors import CompatError
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn


def _settings():
    return get_settings().model_copy(
        update={"chat_analysis_enabled": True, "gcp_project": "p"}
    )


async def _seed_chat_with_message(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hi"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    await chats_repo.add_message(db, session_id=session.id, seq=1, role="user", content="hello")
    await db.flush()
    return session.id


@pytest.mark.asyncio
async def test_rerun_unknown_chat_404(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    with pytest.raises(CompatError) as exc:
        await chat_service.rerun_chat_analysis(
            app_session, _settings(), ids.encode_chat_id(uuid.uuid4())
        )
    assert exc.value.status_code == 404
    await app_session.rollback()


@pytest.mark.asyncio
async def test_rerun_upserts_analysis(app_session, monkeypatch) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_chat_with_message(app_session)

    async def _fake(**kwargs):
        return VertexTurn(text=json.dumps({"chat_summary": "ok", "user_sentiment": "Neutral"}))

    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _fake)
    session = await chat_service.rerun_chat_analysis(
        app_session, _settings(), ids.encode_chat_id(sid)
    )
    assert session.id == sid
    rec = await analyses_repo.get_for_session(app_session, sid)
    assert rec is not None and rec.chat_summary == "ok" and rec.user_sentiment == "Neutral"
    await app_session.rollback()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/compat/test_rerun_chat_service.py -q`
Expected: FAIL — `chat_service` has no attribute `rerun_chat_analysis`.

- [ ] **Step 3: Add `rerun_chat_analysis` to the service**

In `apps/api/src/usan_api/compat/chat_service.py`, replace the existing import line 17 (`from usan_api import telnyx_messaging`) with the combined import:

```python
from usan_api import chat_analysis, telnyx_messaging
```

Then add this function after `get_chat` (after line 168):

```python
async def rerun_chat_analysis(
    db: AsyncSession, settings: Settings, chat_id: str
) -> ChatSession:
    """Recompute the chat's post-chat analysis inline and return the session (the router
    serializes the fresh analysis). 404 if the chat is missing or archived (RLS scopes the
    lookup to the caller's org, so a cross-org chat_id is a clean 404)."""
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    await chat_analysis.analyze_chat_with(db, session.id, settings, force=True)
    await db.commit()
    return session
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest -n0 tests/compat/test_rerun_chat_service.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

Run: `ruff check . && ruff format --check . && uv run mypy`
```bash
git add apps/api/src/usan_api/compat/chat_service.py apps/api/tests/compat/test_rerun_chat_service.py
git commit -m "feat(api): chat_service.rerun_chat_analysis (inline force) (Phase 4c-2)"
```

---

### Task 6: Router — `PUT /rerun-chat-analysis` + analysis loading on get/list + unsupported removal

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/chats.py` (import repo; `_serialize_full` loads analysis; list-chats batches; add the PUT route)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove line 73)
- Modify: `apps/api/tests/compat/conftest.py` (add the `chat_analysis_on` fixture)
- Test: `apps/api/tests/compat/test_rerun_chat_analysis_router.py`

**Interfaces:**
- Consumes: `chat_service.rerun_chat_analysis` (Task 5), `chat_analyses_repo.get_for_session` / `get_for_sessions` (Task 2), `serialize_chat(..., analysis=)` (Task 4).
- Produces: `PUT /rerun-chat-analysis/{chat_id}` (201, `CompatChat`, `exclude_none`); get-chat/list-chats now reflect stored analysis.

- [ ] **Step 1: Add the test fixture**

In `apps/api/tests/compat/conftest.py`, add (after the `gcp_project_set` fixture, ~line 220):

```python
@pytest.fixture
def chat_analysis_on(compat_client: TestClient):
    """Override get_settings on the compat sub-app so the chat-analysis pipeline runs
    (flag on + gcp_project set). Mirrors gcp_project_set."""
    from usan_api.settings import get_settings as _get_settings

    compat_app = _get_compat_app(compat_client)
    base = _get_settings()

    def _override() -> Settings:
        return base.model_copy(
            update={"chat_analysis_enabled": True, "gcp_project": "test-project"}
        )

    compat_app.dependency_overrides[_get_settings] = _override
    yield
    compat_app.dependency_overrides.pop(_get_settings, None)
```

- [ ] **Step 2: Write the failing router test**

Create `apps/api/tests/compat/test_rerun_chat_analysis_router.py`:

```python
"""Phase 4c-2: PUT /rerun-chat-analysis behavior — auth, 404, populate, get/list reflect."""

from __future__ import annotations

import json
import uuid

import pytest

from usan_api.compat import ids
from usan_api.vertex_test import VertexTurn


@pytest.fixture
def patched_vertex(monkeypatch):
    """Mock BOTH Vertex seams: chat_service (create-chat-completion turn) and chat_analysis."""

    async def _reply(**kwargs):
        return VertexTurn(text="agent reply")

    async def _analysis(**kwargs):
        return VertexTurn(
            text=json.dumps(
                {"chat_summary": "A friendly check-in.", "user_sentiment": "Positive",
                 "chat_successful": True}
            )
        )

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _reply)
    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _analysis)


def _seed_chat(compat_client, compat_headers, web_agent_id) -> str:
    chat = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()
    chat_id = chat["chat_id"]
    # One completion turn so the chat has messages to analyze.
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hi there"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return chat_id


def test_rerun_requires_key(compat_client):
    r = compat_client.put(f"/rerun-chat-analysis/{ids.encode_chat_id(uuid.uuid4())}")
    assert r.status_code == 401


def test_rerun_unknown_chat_404(compat_client, compat_headers, chat_analysis_on):
    r = compat_client.put(
        f"/rerun-chat-analysis/{ids.encode_chat_id(uuid.uuid4())}", headers=compat_headers
    )
    assert r.status_code == 404


def test_rerun_populates_and_get_reflects(
    compat_client, compat_headers, web_agent_id, chat_analysis_on, patched_vertex
):
    chat_id = _seed_chat(compat_client, compat_headers, web_agent_id)

    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    analysis = r.json()["chat_analysis"]
    assert analysis["chat_summary"] == "A friendly check-in."
    assert analysis["user_sentiment"] == "Positive"
    assert analysis["chat_successful"] is True

    # get-chat now reflects the stored analysis.
    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers).json()
    assert got["chat_analysis"]["chat_summary"] == "A friendly check-in."

    # list-chats reflects it too (batched load).
    listing = compat_client.post(
        "/v3/list-chats", json={"limit": 50}, headers=compat_headers
    ).json()
    mine = next(c for c in listing["items"] if c["chat_id"] == chat_id)
    assert mine["chat_analysis"]["user_sentiment"] == "Positive"


def test_rerun_archived_chat_404(
    compat_client, compat_headers, web_agent_id, chat_analysis_on
):
    chat = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()
    chat_id = chat["chat_id"]
    assert compat_client.delete(f"/delete-chat/{chat_id}", headers=compat_headers).status_code == 204
    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 404
```

> **Cross-org note:** the rerun's cross-org isolation (org B cannot rerun org A's chat → 404) is the same `get_session`-returns-None-under-RLS path proven by `test_rerun_unknown_chat_404` (a chat invisible to the caller is indistinguishable from a non-existent one) and by the repo-layer RLS test in Task 2. The compat suite is single-org by construction, so a bespoke second-compat-org fixture is intentionally out of scope here — the guarantee is covered at the two cheaper layers.

- [ ] **Step 3: Run it to confirm it fails**

Run: `uv run pytest -n0 tests/compat/test_rerun_chat_analysis_router.py -q`
Expected: FAIL — `PUT /rerun-chat-analysis/{chat_id}` currently 501s (unsupported stub) / `chat_analysis` key missing.

- [ ] **Step 4: Wire the router**

In `apps/api/src/usan_api/compat/routers/chats.py`:

(a) Add the repo import after line 35 (`from usan_api.repositories import chats as chats_repo`):

```python
from usan_api.repositories import chat_analyses as chat_analyses_repo
```

(b) Replace `_serialize_full` (lines 46-48) so it loads the analysis:

```python
async def _serialize_full(db: AsyncSession, session: ChatSession) -> CompatChat:
    messages = await chats_repo.list_messages(db, session.id)
    analysis = await chat_analyses_repo.get_for_session(db, session.id)
    return chat_serializer.serialize_chat(
        session, messages, include_transcript=True, analysis=analysis
    )
```

(c) Replace the `list_chats` handler body (lines 128-130) so it batches the analyses:

```python
    sessions, pagination_key, has_more, total = await chat_service.list_chats(db, body)
    _audit(request, "list-chats")
    analyses = await chat_analyses_repo.get_for_sessions(db, [s.id for s in sessions])
    items = [
        chat_serializer.serialize_chat(
            s, [], include_transcript=False, analysis=analyses.get(s.id)
        )
        for s in sessions
    ]
```

(d) Add the new route (place it after `get_chat`, before `list_chats`, ~line 120):

```python
@router.put(
    "/rerun-chat-analysis/{chat_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def rerun_chat_analysis(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChat:
    session = await chat_service.rerun_chat_analysis(db, settings, chat_id)
    _audit(request, "rerun-chat-analysis", chat_id)
    return await _serialize_full(db, session)
```

- [ ] **Step 5: Remove the unsupported stub**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete line 73 ONLY:

```python
    ("PUT", "/rerun-chat-analysis/{chat_id}"),
```

Leave line 72 `("PUT", "/rerun-call-analysis/{call_id}"),` in place.

- [ ] **Step 6: Run the router test + surface coverage**

Run: `uv run pytest -n0 tests/compat/test_rerun_chat_analysis_router.py tests/compat/test_surface_coverage.py -q`
Expected: PASS — the route is served (surface coverage stays green; `KNOWN_GAPS == frozenset()` unchanged), and the new route behaves.

- [ ] **Step 7: Lint + commit**

Run: `ruff check . && ruff format --check . && uv run mypy`
```bash
git add apps/api/src/usan_api/compat/routers/chats.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/conftest.py apps/api/tests/compat/test_rerun_chat_analysis_router.py
git commit -m "feat(api): serve PUT /rerun-chat-analysis + reflect chat_analysis on get/list (Phase 4c-2)"
```

---

### Task 7: Contract-freeze test + operator docs

**Files:**
- Create: `apps/api/tests/compat/test_freeze_chat_analysis.py`
- Create: `docs/deployment/chat-analysis.md` (same directory the 4c-1 plan used for `chat-agents.md` — confirm with `ls`)
- Test: the freeze file itself.

**Interfaces:**
- Consumes: the served route (Task 6), the `chat_analysis_on` fixture (Task 6), `assert_conforms` / `assert_sdk_roundtrip`.

- [ ] **Step 1: Write the freeze test**

Create `apps/api/tests/compat/test_freeze_chat_analysis.py`:

```python
"""Contract freeze for rerun-chat-analysis (RetellAI parity Phase 4c-2).

The rerun response is a ChatResponse carrying chat_analysis; it must conform to the pinned
oracle schema and round-trip through the retell SDK model. Vertex is mocked (no real LLM).
"""

from __future__ import annotations

import json
import uuid

import pytest

from usan_api.compat import ids
from usan_api.vertex_test import VertexTurn

from .conformance import assert_conforms, assert_sdk_roundtrip


@pytest.fixture
def _mock_vertex(monkeypatch):
    async def _reply(**kwargs):
        return VertexTurn(text="agent reply")

    async def _analysis(**kwargs):
        return VertexTurn(
            text=json.dumps(
                {"chat_summary": "The agent answered the user's question.",
                 "user_sentiment": "Positive", "chat_successful": True}
            )
        )

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _reply)
    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _analysis)


def test_rerun_requires_key(compat_client):
    r = compat_client.put(f"/rerun-chat-analysis/{ids.encode_chat_id(uuid.uuid4())}")
    assert r.status_code == 401


def test_rerun_chat_analysis_conforms(
    compat_client, compat_headers, web_agent_id, chat_analysis_on, _mock_vertex
):
    chat_id = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()["chat_id"]
    assert (
        compat_client.post(
            "/create-chat-completion",
            json={"chat_id": chat_id, "content": "what time is it?"},
            headers=compat_headers,
        ).status_code
        == 201
    )

    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["chat_id"] == chat_id
    assert body["chat_analysis"]["chat_summary"]
    assert body["chat_analysis"]["user_sentiment"] == "Positive"
    assert body["chat_analysis"]["chat_successful"] is True
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")
```

- [ ] **Step 2: Run it to confirm it passes**

Run: `uv run pytest -n0 tests/compat/test_freeze_chat_analysis.py -q`
Expected: PASS (2 tests). If `assert_conforms` fails on a `null` key, a serialized field violated `exclude_none` — fix the serializer, do not loosen the assertion.

- [ ] **Step 3: Write the operator doc**

Confirm the deployment-docs dir: `ls docs/deployment/`. The 4c-1 plan created `docs/deployment/chat-agents.md`; create `docs/deployment/chat-analysis.md` there:

```markdown
# Chat Analysis (Phase 4c-2) — operator note

`PUT /rerun-chat-analysis/{chat_id}` recomputes a chat's post-chat analysis (chat_summary,
user_sentiment, chat_successful) via Vertex and returns the updated chat. It also surfaces
`chat_analysis` on get-chat / list-chats.

**Ships inert.** No analysis runs until BOTH:

- `CHAT_ANALYSIS_ENABLED=true` (default `false`), AND
- `GCP_PROJECT` is set (the Vertex project; ADC service account on the VM).

Optional: `CHAT_ANALYSIS_MODEL` (default `gemini-2.5-flash`).

With the flag off or `GCP_PROJECT` unset, the endpoint still returns 201 with the chat and
any previously stored analysis — it just performs no recompute (no spend, no PHI egress).

PHI: the transcript is sent ONLY to Vertex via ADC (BAA-covered), never the Gemini Developer
API; the analysis persists only to BAA Postgres (`chat_analyses`, RLS-isolated per org).

Migration `0046` (the `chat_analyses` table) must be applied (owner-DDL) on deploy.
```

- [ ] **Step 4: Commit**

```bash
git add apps/api/tests/compat/test_freeze_chat_analysis.py docs/deployment/chat-analysis.md
git commit -m "test(api): freeze rerun-chat-analysis ChatResponse + operator doc (Phase 4c-2)"
```

---

## Final verification (after Task 7, before the whole-branch review)

- [ ] Run the full suite: `uv run pytest -q` (suite default `-n auto`). Expected: all pass (a heavy-load testcontainer flake on UNRELATED tests is a known false alarm — re-run `-n0` to confirm any failure is in scope).
- [ ] `ruff check . && ruff format --check . && uv run mypy` — all clean.
- [ ] `uv run alembic heads` — single head `0046`.
- [ ] Confirm `KNOWN_GAPS` in `tests/compat/test_surface_coverage.py` is still `frozenset()` and the file is unmodified.

## Completion

Ends at squash-merge to `main` (on the user's explicit go-ahead), **no `v* tag`**, migration `0046` inert until an operator deploys + mints a compat key. After the final whole-branch review passes, use superpowers:finishing-a-development-branch (push + open PR; the user squash-merges).
