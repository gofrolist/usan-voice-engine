# RetellAI Parity Phase 7 slice 2 — `v2/list-retell-llms` + `rerun-call-analysis` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `GET /v2/list-retell-llms` (paginated keyset list) and `PUT /rerun-call-analysis/{call_id}` (force-recompute post-call analysis, 201 → full call) from 501 stubs to served, keeping `KNOWN_GAPS = frozenset()`.

**Architecture:** The v2 list is a structural port of 6a's `GET /v2/list-conversation-flows` (keyset `created_at|id` cursor over `agent_profiles`, channel-agnostic). Rerun adds `force=True` semantics to the existing `summarization.summarize_call_with` (upsert into `conversation_summaries`, skip fact extraction, flush-only) behind a never-raises compat handler that mirrors `rerun-chat-analysis`. One migration (0051: `conversation_summaries.contact_id` nullable, for contact-less web calls).

**Tech Stack:** FastAPI compat sub-app, SQLAlchemy async + Postgres RLS, Alembic, Vertex (`run_vertex_turn`, mocked in tests), pytest + oracle conformance (`assert_conforms`/`assert_sdk_roundtrip`), retell-sdk round-trip.

**Spec:** `docs/superpowers/specs/2026-07-01-retell-parity-phase7-slice2-llm-list-rerun-analysis-design.md`

## Global Constraints

- All commands run from `apps/api` (`cd apps/api` first). Tests: `uv run pytest` (parallel by default). Single test file: `uv run pytest tests/path.py -v`.
- Type check with `uv run mypy` (NO path argument — `uv run mypy .` wrongly pulls in tests, ~3576 pre-existing errors). Lint: `uv run ruff check . && uv run ruff format .`.
- The RetellAI oracle **omits optional fields when null** — never serialize `null` for an absent optional field. Routes use `response_model_exclude_none=True`; hand-built dicts only set keys that have values.
- PHI containment: never log transcript/summary text or E.164 numbers; on errors log `type(exc).__name__` only. Vertex only via `run_vertex_turn` (`vertexai=True` + ADC).
- Migration is owner-DDL, additive, single Alembic head `0051` after this slice.
- Commit format: `type(api): description` (e.g. `feat(api): ...`, `test(api): ...`). Frequent commits — one per task minimum.
- Do NOT touch the root `GET /list-retell-llms` (bare array) — it is a separate frozen op pinned by `tests/compat/test_freeze_agents.py::test_list_retell_llms_is_bare_array_at_root`.
- `tests/compat/test_surface_coverage.py` derives served ops from the live route table and `tests/test_compat_fidelity.py`'s 501 list uses other stubs — neither file hardcodes these two paths, so only `unsupported.py` changes for the 501→served moves (verify with the greps in Task 7).

---

### Task 1: Migration 0051 — `conversation_summaries.contact_id` nullable

**Files:**
- Create: `apps/api/migrations/versions/0051_summary_contact_nullable.py`
- Modify: `apps/api/src/usan_api/db/models.py:345-347` (ConversationSummary.contact_id)
- Test: `apps/api/tests/test_summarization.py` (append)

**Interfaces:**
- Produces: `conversation_summaries.contact_id` accepts NULL (DB + ORM). Task 2's `upsert` and Task 3's force path rely on inserting `contact_id=None`.

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_summarization.py` (it already has the `session_factory` fixture and imports `text` from sqlalchemy):

```python
async def test_contact_id_is_nullable(session_factory):
    """Migration 0051 pin: a contact-less (web-call) summary row must be insertable."""
    async with session_factory() as db:
        nullable = (
            await db.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'conversation_summaries' AND column_name = 'contact_id'"
                )
            )
        ).scalar_one()
    assert nullable == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_summarization.py::test_contact_id_is_nullable -v`
Expected: FAIL with `assert 'NO' == 'YES'`

- [ ] **Step 3: Write the migration**

Create `apps/api/migrations/versions/0051_summary_contact_nullable.py`:

```python
"""conversation_summaries.contact_id: allow NULL (Phase 7 slice 2, rerun-call-analysis).

A compat rerun of a contact-less web call persists a summary row with contact_id NULL.
The next-call built-ins read summaries via get_latest(contact_id=...), so NULL rows can
never feed them. Owner-DDL (ALTER TABLE runs as the usan owner on deploy); additive/inert.

Revision ID: 0051
Revises: 0050
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "conversation_summaries",
        "contact_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "conversation_summaries",
        "contact_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
```

In `apps/api/src/usan_api/db/models.py`, change the `ConversationSummary.contact_id` mapping (currently lines 345-347) from:

```python
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
```

to:

```python
    # Nullable since 0051: a compat rerun-call-analysis of a contact-less web call stores
    # a summary with no contact; get_latest(contact_id=...) never surfaces NULL rows.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_summarization.py::test_contact_id_is_nullable -v`
Expected: PASS (the test-DB bootstrap applies migrations). Also run `uv run alembic heads` — expected single head: `0051`.

- [ ] **Step 5: Verify no regressions in the file + mypy**

Run: `uv run pytest tests/test_summarization.py -v` → all PASS.
Run: `uv run mypy` → no NEW errors. (The `uuid.UUID | None` widening may surface callers; `summarization.py` passes `call.contact_id` — itself `uuid.UUID | None` on the Call model — into `create(...)`, whose `contact_id: uuid.UUID` parameter is only reached on the non-force path where a None contact already bailed. If mypy flags it at this task, widen `create`'s `contact_id` annotation to `uuid.UUID | None` and note the runtime guard in its docstring; otherwise leave `create` untouched.)

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0051_summary_contact_nullable.py src/usan_api/db/models.py tests/test_summarization.py
git commit -m "feat(api): migration 0051 — conversation_summaries.contact_id nullable"
```

---

### Task 2: `conversation_summaries.upsert` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/conversation_summaries.py`
- Test: `apps/api/tests/test_summarization.py` (append)

**Interfaces:**
- Consumes: nullable `contact_id` from Task 1.
- Produces: `async def upsert(db, *, call_id: uuid.UUID, contact_id: uuid.UUID | None, summary: str, open_plans: list[Any], model_version: str) -> ConversationSummary` — insert-or-replace on the unique `call_id`; flush-only. Task 3 calls it on the force path.

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_summarization.py` (uses the file's existing `client`, `session_factory`, `mock_dispatch` fixtures and `_make_contact`/`_enqueue_call` helpers):

```python
async def test_upsert_inserts_then_replaces(client, session_factory, mock_dispatch):
    from usan_api.repositories import conversation_summaries as summaries_repo

    contact_id = await _make_contact(session_factory)
    call_id = uuid.UUID(_enqueue_call(client, contact_id))

    async with session_factory() as db:
        first = await summaries_repo.upsert(
            db,
            call_id=call_id,
            contact_id=uuid.UUID(contact_id),
            summary="first",
            open_plans=["plan a"],
            model_version="m1",
        )
        await db.commit()
    assert first.summary == "first"

    async with session_factory() as db:
        second = await summaries_repo.upsert(
            db,
            call_id=call_id,
            contact_id=uuid.UUID(contact_id),
            summary="second",
            open_plans=[],
            model_version="m2",
        )
        await db.commit()
    assert second.summary == "second"
    assert second.open_plans == []
    assert second.model_version == "m2"

    async with session_factory() as db:
        row = await summaries_repo.get_for_call(db, call_id)
    assert row is not None
    assert row.summary == "second"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_summarization.py::test_upsert_inserts_then_replaces -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'upsert'`

- [ ] **Step 3: Implement `upsert`**

Append to `apps/api/src/usan_api/repositories/conversation_summaries.py`:

```python
async def upsert(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    summary: str,
    open_plans: list[Any],
    model_version: str,
) -> ConversationSummary:
    """Insert-or-replace the call's summary (compat rerun-call-analysis force path).

    ON CONFLICT (call_id) DO UPDATE replaces summary/open_plans/model_version and leaves
    call_id/contact_id/organization_id untouched on an existing row, so a rerun can never
    relink a summary. ``contact_id`` is None for contact-less web calls (0051). Flush-only;
    the caller commits.
    """
    stmt = (
        pg_insert(ConversationSummary)
        .values(
            call_id=call_id,
            contact_id=contact_id,
            summary=summary,
            open_plans=open_plans,
            model_version=model_version,
        )
        .on_conflict_do_update(
            index_elements=[ConversationSummary.call_id],
            set_={
                "summary": summary,
                "open_plans": open_plans,
                "model_version": model_version,
            },
        )
    )
    await db.execute(stmt)
    await db.flush()
    row = await get_for_call(db, call_id)
    if row is None:  # pragma: no cover - the upsert above guarantees the row exists
        raise RuntimeError("conversation summary upsert did not persist")
    return row
```

(`pg_insert`, `ConversationSummary`, `get_for_call`, `uuid`, `Any`, `AsyncSession` are all already imported in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_summarization.py::test_upsert_inserts_then_replaces -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usan_api/repositories/conversation_summaries.py tests/test_summarization.py
git commit -m "feat(api): conversation_summaries.upsert for analysis rerun"
```

---

### Task 3: `summarize_call_with(force=True)` — recompute semantics

**Files:**
- Modify: `apps/api/src/usan_api/summarization.py:145-208` (`summarize_call_with`)
- Test: `apps/api/tests/test_summarization.py` (append)

**Interfaces:**
- Consumes: `conversation_summaries.upsert` (Task 2).
- Produces: `async def summarize_call_with(db, call_id: uuid.UUID, settings: Settings, *, force: bool = False) -> ConversationSummary | None`. Semantics Task 4 relies on — `force=True`: recompute-even-if-summarized (upsert), contact-less calls allowed, NO fact persistence, **flush-only** (caller commits), `call_analyzed` still enqueued; raises on Vertex failure (the compat handler catches). `force=False`: byte-identical to today (internal commit, background trigger untouched).

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_summarization.py`. The file already defines `_vertex_returning(payload: dict) -> AsyncMock` (line ~112) — use it with `monkeypatch.setattr(summarization, "run_vertex_turn", _vertex_returning({...}))`, matching the existing tests:

```python
async def _seeded_call(client, session_factory) -> tuple[str, str]:
    """A queued call with a transcript; returns (call_id, contact_id) as strings."""
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(
        session_factory, call_id, [("user", "I feel great"), ("agent", "Wonderful!")]
    )
    return call_id, contact_id


async def test_force_recomputes_and_replaces(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _contact_id = await _seeded_call(client, session_factory)
    settings = get_settings()

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "v1",
                "open_plans": [],
                "facts": [{"category": "preference", "content": "likes tea"}],
            }
        ),
    )
    async with session_factory() as db:
        row = await summarization.summarize_call_with(db, uuid.UUID(call_id), settings)
    assert row is not None and row.summary == "v1"

    # Non-force is idempotent: a second normal run is a no-op.
    async with session_factory() as db:
        assert await summarization.summarize_call_with(db, uuid.UUID(call_id), settings) is None

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "v2",
                "open_plans": ["call the doctor"],
                "facts": [{"category": "preference", "content": "likes coffee"}],
            }
        ),
    )
    async with session_factory() as db:
        forced = await summarization.summarize_call_with(
            db, uuid.UUID(call_id), settings, force=True
        )
        await db.commit()  # force is flush-only; the caller commits
    assert forced is not None
    assert forced.summary == "v2"
    assert forced.open_plans == ["call the doctor"]

    # Facts are NOT persisted on force: only the v1 fact exists.
    async with session_factory() as db:
        contents = (
            (await db.execute(text("SELECT content FROM personal_facts"))).scalars().all()
        )
    assert "likes tea" in contents
    assert "likes coffee" not in contents


async def test_force_is_flush_only(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _ = await _seeded_call(client, session_factory)
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "uncommitted", "open_plans": [], "facts": []}),
    )

    async with session_factory() as db:
        await summarization.summarize_call_with(
            db, uuid.UUID(call_id), get_settings(), force=True
        )
        # NO commit — the row must not be visible from another session.
        async with session_factory() as other:
            from usan_api.repositories import conversation_summaries as summaries_repo

            assert await summaries_repo.get_for_call(other, uuid.UUID(call_id)) is None


async def test_force_contactless_call(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _ = await _seeded_call(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            text("UPDATE calls SET contact_id = NULL WHERE id = CAST(:c AS uuid)"),
            {"c": call_id},
        )
        await db.commit()

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "web recap", "open_plans": [], "facts": []}),
    )
    async with session_factory() as db:
        row = await summarization.summarize_call_with(
            db, uuid.UUID(call_id), get_settings(), force=True
        )
        await db.commit()
    assert row is not None
    assert row.summary == "web recap"
    assert row.contact_id is None

    # And the non-force path still bails on contact-less calls (unchanged behavior).
    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM conversation_summaries WHERE call_id = CAST(:c AS uuid)"),
            {"c": call_id},
        )
        await db.commit()
    async with session_factory() as db:
        assert (
            await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())
            is None
        )


async def test_force_reenqueues_call_analyzed(client, session_factory, mock_dispatch, monkeypatch):
    """The rerun re-fires the compat call_analyzed webhook event (oracle-faithful)."""
    call_id, _ = await _seeded_call(client, session_factory)
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "s", "open_plans": [], "facts": []}),
    )
    enqueued = AsyncMock()
    monkeypatch.setattr("usan_api.compat.lifecycle.enqueue_compat_call_event", enqueued)
    async with session_factory() as db:
        await summarization.summarize_call_with(
            db, uuid.UUID(call_id), get_settings(), force=True
        )
        await db.commit()
    enqueued.assert_awaited_once()
    assert enqueued.await_args.kwargs["event"] == "call_analyzed"
```

(`summarization`, `get_settings`, `AsyncMock`, `json`, `uuid`, `text` are already imported at the top of this file; add any that are missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_summarization.py -v -k force`
Expected: FAIL with `TypeError: summarize_call_with() got an unexpected keyword argument 'force'`

- [ ] **Step 3: Implement the force path**

Rewrite `summarize_call_with` in `apps/api/src/usan_api/summarization.py`. The current body (idempotency check → contact bail → transcript render → Vertex turn → `summaries_repo.create` → facts loop → `enqueue_compat_call_event` → `db.commit()`) becomes:

```python
async def summarize_call_with(
    db: AsyncSession, call_id: uuid.UUID, settings: Settings, *, force: bool = False
) -> ConversationSummary | None:
    """Summarize one call and persist the recap + extracted facts. Returns the summary
    row, or None when there is nothing to do (already summarized / no transcript).

    ``force=True`` (compat rerun-call-analysis) recomputes even when a summary already
    exists (upsert-replace), covers contact-less web calls (contact_id NULL, 0051), skips
    personal-fact extraction (a rerun must never duplicate facts), and is FLUSH-ONLY —
    the caller owns the commit. ``force=False`` keeps the original background-trigger
    behavior unchanged, including the internal commit.
    """
    if not force and await summaries_repo.get_for_call(db, call_id) is not None:
        return None  # idempotent: a prior trigger already summarized this call
    call = await calls_repo.get_call(db, call_id)
    if call is None or (call.contact_id is None and not force):
        return None
    segments = await transcripts_repo.list_for_call(db, call_id)
    transcript_text = _render_transcript(segments)
    if not transcript_text:
        return None  # no transcript yet -> nothing to summarize, no Vertex call

    turn = await run_vertex_turn(
        model=settings.summarization_model,
        temperature=0.2,
        system_instruction=_SYSTEM_INSTRUCTION,
        tools=[],
        contents=[{"role": "user", "parts": [{"text": transcript_text}]}],
        settings=settings,
    )
    parsed = _parse_summary(turn.text)

    if force:
        summary_row: ConversationSummary | None = await summaries_repo.upsert(
            db,
            call_id=call_id,
            contact_id=call.contact_id,
            summary=parsed.summary,
            open_plans=parsed.open_plans,
            model_version=settings.summarization_model,
        )
    else:
        summary_row = await summaries_repo.create(
            db,
            call_id=call_id,
            contact_id=call.contact_id,
            summary=parsed.summary,
            open_plans=parsed.open_plans,
            model_version=settings.summarization_model,
        )
    if summary_row is None:
        # Lost a race to a concurrent trigger — it owns the facts too; don't double-write.
        return None

    if not force:
        # Extracted facts, skipping ones already active for this contact (avoid re-adding
        # the same fact every call). source='extracted' — never forge a contact_stated fact
        # here. Dedup against the FULL active key set (uncapped) — list_active's 50-row
        # injection cap would let a duplicate beyond row 50 be re-inserted every call
        # (unbounded growth). Skipped entirely on force: a rerun re-extracting facts would
        # duplicate them, and a contact-less call has no fact target.
        existing = await personal_facts_repo.list_active_keys(db, contact_id=call.contact_id)
        for fact in parsed.facts:
            if (fact.category, fact.content) in existing:
                continue
            existing.add((fact.category, fact.content))
            await personal_facts_repo.create(
                db,
                contact_id=call.contact_id,
                category=fact.category,
                content=fact.content,
                structured=fact.structured or None,
                source="extracted",
            )
    # Feature 003 / US2: the summary IS the 'analyzed' signal — emit a compat call_analyzed
    # webhook for an agent with a subscription (no-op otherwise), in the SAME transaction as
    # the recap so a rolled-back summarize emits nothing. Local import avoids a compat
    # import-time dependency in this native module.
    from usan_api.compat.lifecycle import enqueue_compat_call_event

    await enqueue_compat_call_event(db, call, event="call_analyzed")
    if force:
        # Flush-only on the rerun path: the compat router owns the transaction.
        logger.bind(call_id=str(call_id)).info("Reran call analysis")
        return summary_row
    await db.commit()
    logger.bind(call_id=str(call_id), facts=len(parsed.facts)).info("Summarized call")
    return summary_row
```

Keep the surrounding comments/code identical where shown — this is a surgical extension, not a rewrite of the module.

- [ ] **Step 4: Run tests to verify they pass (including all pre-existing ones)**

Run: `uv run pytest tests/test_summarization.py -v`
Expected: ALL PASS (force tests new-green, pre-existing non-force tests untouched).

- [ ] **Step 5: mypy + ruff**

Run: `uv run mypy && uv run ruff check . && uv run ruff format .`
Expected: clean (no new errors).

- [ ] **Step 6: Commit**

```bash
git add src/usan_api/summarization.py tests/test_summarization.py
git commit -m "feat(api): summarize_call_with force=True — upsert recompute, facts skipped, flush-only"
```

---

### Task 4: `PUT /rerun-call-analysis/{call_id}` compat endpoint

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/calls.py` (new handler + import)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the stub tuple)
- Modify: `apps/api/tests/compat/conftest.py` (new `summarization_on` fixture)
- Create: `apps/api/tests/compat/test_rerun_call_analysis.py`

**Interfaces:**
- Consumes: `summarization.summarize_call_with(db, call.id, settings, force=True)` (Task 3) — flush-only, raises on Vertex failure. `_load_call(db, call_id)` (existing, 404 missing/archived). `call_serializer.serialize_call(db, call, settings, client_host=...)` (existing).
- Produces: served `PUT /rerun-call-analysis/{call_id}` → 201 `CompatCall` (exclude_none). Fixture `summarization_on` for compat tests.

- [ ] **Step 1: Write the failing tests**

Add to `apps/api/tests/compat/conftest.py`, next to the existing `chat_analysis_on` fixture (same shape):

```python
@pytest.fixture
def summarization_on(compat_client: TestClient):
    """Override get_settings on the compat sub-app: summarization enabled + gcp_project.

    Same dependency_overrides key as gcp_project_set/chat_analysis_on — never combine
    two of these fixtures in one test.
    """
    from usan_api.settings import get_settings as _get_settings

    compat_app = _get_compat_app(compat_client)
    base = _get_settings()

    def _override() -> Settings:
        return base.model_copy(
            update={"summarization_enabled": True, "gcp_project": "test-project"}
        )

    compat_app.dependency_overrides[_get_settings] = _override
    yield
    compat_app.dependency_overrides.pop(_get_settings, None)
```

Create `apps/api/tests/compat/test_rerun_call_analysis.py`:

```python
"""Phase 7 slice 2: PUT /rerun-call-analysis/{call_id} — 201 V2CallResponse, best-effort.

404 only for missing/archived; unconfigured / transcript-less / Vertex-failure all keep
the prior analysis and still answer 201 (mirrors rerun-chat-analysis). The recompute
upserts conversation_summaries, which get-call then reflects.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import _published_agent_id, create_call
from usan_api import summarization
from usan_api.compat import ids
from usan_api.vertex_test import VertexTurn


def _vertex_returns(monkeypatch, summary: str) -> None:
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        AsyncMock(
            return_value=VertexTurn(
                text=json.dumps({"summary": summary, "open_plans": [], "facts": []})
            )
        ),
    )


async def _add_transcript(async_database_url: str, call_id: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await db.execute(
                text(
                    "INSERT INTO transcripts (call_id, role, content, started_at) "
                    "VALUES (CAST(:c AS uuid), 'user', 'I feel great today', now())"
                ),
                {"c": str(ids.decode_call_id(call_id))},
            )
            await db.commit()
    finally:
        await engine.dispose()


def _seed_call(compat_client, compat_headers) -> str:
    agent_id = _published_agent_id(compat_client, compat_headers)
    r = create_call(compat_client, compat_headers, override_agent_id=agent_id)
    assert r.status_code == 201, r.text
    return r.json()["call_id"]


def test_rerun_requires_key(compat_client):
    r = compat_client.put(f"/rerun-call-analysis/{ids.encode_call_id(uuid.uuid4())}")
    assert r.status_code == 401


def test_rerun_unknown_call_404(compat_client, compat_headers):
    r = compat_client.put(
        f"/rerun-call-analysis/{ids.encode_call_id(uuid.uuid4())}", headers=compat_headers
    )
    assert r.status_code == 404


@pytest.mark.frozen
async def test_rerun_populates_analysis_and_conforms(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours,
    summarization_on, monkeypatch, async_database_url,
):
    call_id = _seed_call(compat_client, compat_headers)
    await _add_transcript(async_database_url, call_id)

    _vertex_returns(monkeypatch, "A cheerful check-in.")
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["call_analysis"]["call_summary"] == "A cheerful check-in."
    assert_conforms(body, "V2PhoneCallResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneCallResponse")

    # get-call reflects the persisted analysis.
    g = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers).json()
    assert g["call_analysis"]["call_summary"] == "A cheerful check-in."

    # A second rerun REPLACES it (upsert, not insert-once).
    _vertex_returns(monkeypatch, "Actually a somber check-in.")
    r2 = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r2.status_code == 201
    assert r2.json()["call_analysis"]["call_summary"] == "Actually a somber check-in."


def test_rerun_unconfigured_still_201(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    """No summarization_on override: flag off -> no Vertex call, prior (absent) analysis."""
    call_id = _seed_call(compat_client, compat_headers)
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()  # non-terminal call, no summary -> omitted


def test_rerun_without_transcript_still_201(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours,
    summarization_on, monkeypatch,
):
    call_id = _seed_call(compat_client, compat_headers)
    _vertex_returns(monkeypatch, "never used")
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()


async def test_rerun_vertex_failure_keeps_prior_and_201(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours,
    summarization_on, monkeypatch, async_database_url,
):
    call_id = _seed_call(compat_client, compat_headers)
    await _add_transcript(async_database_url, call_id)
    monkeypatch.setattr(
        summarization, "run_vertex_turn", AsyncMock(side_effect=RuntimeError("boom"))
    )
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()

    # And the endpoint still works on a later request (session not poisoned).
    g = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers)
    assert g.status_code == 200


@pytest.mark.frozen
async def test_rerun_web_call_conforms(
    compat_client, compat_headers, web_agent_id, mock_web_dispatch,
    summarization_on, monkeypatch, async_database_url,
):
    """Contact-less web-call rerun: recompute persists (contact_id NULL) and the 201 body
    conforms to the WEB branch of V2CallResponse."""
    r = compat_client.post(
        "/v2/create-web-call", json={"agent_id": web_agent_id}, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    call_id = r.json()["call_id"]
    await _add_transcript(async_database_url, call_id)

    _vertex_returns(monkeypatch, "A web chat recap.")
    rr = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert rr.status_code == 201, rr.text
    body = rr.json()
    assert body["call_analysis"]["call_summary"] == "A web chat recap."
    assert_conforms(body, "V2WebCallResponse")
    assert_sdk_roundtrip(body, "retell.types:WebCallResponse")
```

(`mock_dispatch`, `allow_quiet_hours`, `web_agent_id`, `mock_web_dispatch` are all existing fixtures in `tests/compat/conftest.py`; `async_database_url` comes from the top-level `tests/conftest.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_rerun_call_analysis.py -v`
Expected: FAIL — the 501 stub answers `PUT /rerun-call-analysis/...` (assert 201 == 501), and unknown-call gets 501 not 404.

- [ ] **Step 3: Implement the handler + remove the stub**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete these two lines (the group comment too, since the group becomes empty):

```python
    # --- Analysis re-run ---
    ("PUT", "/rerun-call-analysis/{call_id}"),
```

In `apps/api/src/usan_api/compat/routers/calls.py`, change the existing import line

```python
from usan_api import livekit_dispatch
```

to

```python
from usan_api import livekit_dispatch, summarization
```

and add the handler after `get_call`:

```python
@router.put(
    "/rerun-call-analysis/{call_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatCall,
    response_model_exclude_none=True,
)
async def rerun_call_analysis(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatCall:
    """PUT /rerun-call-analysis — recompute the call's analysis, return the call (201).

    Best-effort (mirrors rerun-chat-analysis): 404 only for a missing/archived call.
    Unconfigured summarization, a transcript-less call, or a Vertex failure all leave the
    prior analysis in place and still answer 201. The recompute (summary upsert +
    call_analyzed enqueue) is flush-only; this handler owns the commit, and a failure
    rolls the whole recompute back (the after_begin listener re-applies the org RLS
    context on the next transaction, so the serialization below still reads real rows).
    """
    call = await _load_call(db, call_id)
    if settings.summarization_enabled and settings.gcp_project:
        try:
            await summarization.summarize_call_with(db, call.id, settings, force=True)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - never break the rerun; log TYPE only
            logger.bind(call_id=call_id, err=type(exc).__name__).error(
                "call analysis rerun crashed"
            )
            await db.rollback()
    _audit(request, "rerun-call-analysis", call_id)
    call = await _load_call(db, call_id)  # re-read after commit/rollback (expired state)
    return await call_serializer.serialize_call(db, call, settings, client_host=client_ip(request))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_rerun_call_analysis.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Surface + regression check**

Run: `uv run pytest tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py tests/compat/test_freeze_calls.py tests/compat/test_delete_call.py -v`
Expected: ALL PASS (rerun now served at the exact oracle path; no stale 501 references).

- [ ] **Step 6: mypy + ruff, then commit**

```bash
uv run mypy && uv run ruff check . && uv run ruff format .
git add src/usan_api/compat/routers/calls.py src/usan_api/compat/routers/unsupported.py tests/compat/conftest.py tests/compat/test_rerun_call_analysis.py
git commit -m "feat(api): PUT /rerun-call-analysis — 501 -> served (201 V2CallResponse, best-effort recompute)"
```

---

### Task 5: Retell-LLM keyset cursor codec + `agent_profiles` keyset query

**Files:**
- Modify: `apps/api/src/usan_api/compat/ids.py` (append two functions)
- Modify: `apps/api/src/usan_api/repositories/agent_profiles.py` (append one function)
- Create: `apps/api/tests/compat/test_list_retell_llms_v2.py` (codec tests only in this task)

**Interfaces:**
- Produces: `ids.encode_retell_llm_cursor(created_at: datetime, pid: uuid.UUID) -> str` / `ids.decode_retell_llm_cursor(token: str) -> tuple[datetime, uuid.UUID]` (raises `CompatError(422)` on bad input); `agent_profiles.list_profiles_keyset(db, *, limit: int, descending: bool, after: tuple[datetime, uuid.UUID] | None) -> list[AgentProfile]` (fetches `limit+1`, archived excluded, channel-agnostic). Task 6's router consumes both.

- [ ] **Step 1: Write the failing codec tests**

Create `apps/api/tests/compat/test_list_retell_llms_v2.py`:

```python
"""Phase 7 slice 2: GET /v2/list-retell-llms — keyset cursor codec + paginated list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError


def test_cursor_roundtrip():
    now = datetime.now(UTC)
    pid = uuid.uuid4()
    token = ids.encode_retell_llm_cursor(now, pid)
    assert ids.decode_retell_llm_cursor(token) == (now, pid)


def test_bad_cursor_raises_422():
    with pytest.raises(CompatError) as exc:
        ids.decode_retell_llm_cursor("not-a-cursor")
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_list_retell_llms_v2.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'encode_retell_llm_cursor'`

- [ ] **Step 3: Implement codec + repo query**

Append to `apps/api/src/usan_api/compat/ids.py` (next to the other cursor codecs, which delegate to the existing `_encode_keyset_cursor`/`_decode_keyset_cursor` helpers):

```python
def encode_retell_llm_cursor(created_at: datetime, pid: uuid.UUID) -> str:
    """Opaque (created_at, id) keyset cursor (delegates to the shared helper)."""
    return _encode_keyset_cursor(created_at, pid)


def decode_retell_llm_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor token back to (created_at, id). Raises CompatError(422) on any bad input."""
    return _decode_keyset_cursor(token)
```

Append to `apps/api/src/usan_api/repositories/agent_profiles.py` (imports already present in this module: `select`, `AgentProfile`, `ProfileStatus`, `AsyncSession`, `uuid`; add `and_`, `or_` to the existing `from sqlalchemy import ...` line and change `from datetime import time` to `from datetime import datetime, time`):

```python
async def list_profiles_keyset(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[AgentProfile]:
    """Keyset-paginate non-archived profiles over (created_at, id) — v2 list-retell-llms.

    Channel-agnostic on purpose (a Retell-LLM is channel-agnostic infra, 4c-1). RLS scopes
    to the caller's org. Fetches limit+1 so the caller computes has_more without a COUNT.
    """
    stmt = select(AgentProfile).where(AgentProfile.status != ProfileStatus.ARCHIVED)
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    AgentProfile.created_at < after_created_at,
                    and_(
                        AgentProfile.created_at == after_created_at,
                        AgentProfile.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    AgentProfile.created_at > after_created_at,
                    and_(
                        AgentProfile.created_at == after_created_at,
                        AgentProfile.id > after_id,
                    ),
                )
            )
    order = (
        (AgentProfile.created_at.desc(), AgentProfile.id.desc())
        if descending
        else (AgentProfile.created_at.asc(), AgentProfile.id.asc())
    )
    stmt = stmt.order_by(*order).limit(limit + 1)
    result = await db.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_list_retell_llms_v2.py -v`
Expected: PASS (2 codec tests).

- [ ] **Step 5: mypy + ruff, then commit**

```bash
uv run mypy && uv run ruff check . && uv run ruff format .
git add src/usan_api/compat/ids.py src/usan_api/repositories/agent_profiles.py tests/compat/test_list_retell_llms_v2.py
git commit -m "feat(api): retell-llm keyset cursor codec + agent_profiles keyset query"
```

---

### Task 6: `GET /v2/list-retell-llms` endpoint

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/retell_llm.py` (new handler + imports)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the stub tuple)
- Modify: `apps/api/tests/compat/test_list_retell_llms_v2.py` (append endpoint tests)

**Interfaces:**
- Consumes: `ids.encode/decode_retell_llm_cursor`, `agent_profiles_repo.list_profiles_keyset` (Task 5), `agent_bridge.serialize_llm` (existing).
- Produces: served `GET /v2/list-retell-llms` → `{items, has_more[, pagination_key]}`.

- [ ] **Step 1: Write the failing endpoint tests**

Append to `apps/api/tests/compat/test_list_retell_llms_v2.py`:

```python
from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip


def _make_llm(compat_client, compat_headers, prompt: str) -> str:
    r = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": prompt},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["llm_id"]


def test_v2_list_requires_key(compat_client):
    assert compat_client.get("/v2/list-retell-llms").status_code == 401


@pytest.mark.frozen
def test_v2_list_conforms_and_roundtrips(compat_client, compat_headers):
    for i in range(3):
        _make_llm(compat_client, compat_headers, f"prompt {i}")
    r = compat_client.get("/v2/list-retell-llms?limit=2", headers=compat_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_more"] is True
    assert isinstance(body["pagination_key"], str)
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert_conforms(item, "RetellLLMResponse")
    assert_sdk_roundtrip(body, "retell.types:LlmListResponse")


@pytest.mark.frozen
def test_v2_list_last_page_omits_pagination_key(compat_client, compat_headers):
    _make_llm(compat_client, compat_headers, "solo")
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    assert body["has_more"] is False
    assert "pagination_key" not in body  # RetellAI omit-nulls


def test_v2_list_keyset_walk_no_duplicates(compat_client, compat_headers):
    made = {_make_llm(compat_client, compat_headers, f"p{i}") for i in range(5)}
    seen: list[str] = []
    key: str | None = None
    for _ in range(10):
        url = "/v2/list-retell-llms?limit=2" + (f"&pagination_key={key}" if key else "")
        body = compat_client.get(url, headers=compat_headers).json()
        seen.extend(item["llm_id"] for item in body["items"])
        if not body["has_more"]:
            break
        key = body["pagination_key"]
    assert len(seen) == len(set(seen))
    assert made <= set(seen)


def test_v2_list_ascending_is_reverse_of_descending(compat_client, compat_headers):
    for i in range(3):
        _make_llm(compat_client, compat_headers, f"s{i}")
    desc = compat_client.get("/v2/list-retell-llms?limit=1000", headers=compat_headers).json()
    asc = compat_client.get(
        "/v2/list-retell-llms?limit=1000&sort_order=ascending", headers=compat_headers
    ).json()
    assert [i["llm_id"] for i in asc["items"]] == [i["llm_id"] for i in desc["items"]][::-1]


def test_v2_list_bad_cursor_falls_back_to_first_page(compat_client, compat_headers):
    _make_llm(compat_client, compat_headers, "anchor")
    first = compat_client.get("/v2/list-retell-llms?limit=2", headers=compat_headers).json()
    lenient = compat_client.get(
        "/v2/list-retell-llms?limit=2&pagination_key=garbage", headers=compat_headers
    ).json()
    assert [i["llm_id"] for i in lenient["items"]] == [i["llm_id"] for i in first["items"]]


def test_v2_list_excludes_deleted(compat_client, compat_headers):
    keep = _make_llm(compat_client, compat_headers, "keep")
    gone = _make_llm(compat_client, compat_headers, "gone")
    assert (
        compat_client.delete(f"/delete-retell-llm/{gone}", headers=compat_headers).status_code
        == 204
    )
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    listed = {i["llm_id"] for i in body["items"]}
    assert keep in listed
    assert gone not in listed


def test_v2_list_includes_chat_bound_llm(compat_client, compat_headers):
    """A Retell-LLM is channel-agnostic infra: a chat-agent-bound LLM must still appear."""
    llm_id = _make_llm(compat_client, compat_headers, "chat llm")
    r = compat_client.post(
        "/create-chat-agent",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_id}},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    assert llm_id in {i["llm_id"] for i in body["items"]}
```

(If `create-chat-agent` requires more fields, copy the minimal body from `tests/compat/test_chat_agent_isolation.py` — it exercises the same channel-agnostic invariant against the root list.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compat/test_list_retell_llms_v2.py -v`
Expected: the new endpoint tests FAIL with 501 responses; the two codec tests still PASS.

- [ ] **Step 3: Implement the handler + remove the stub**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete these two lines:

```python
    # --- Retell LLM ---
    ("GET", "/v2/list-retell-llms"),
```

In `apps/api/src/usan_api/compat/routers/retell_llm.py`, extend the imports:

```python
import contextlib
from typing import Any, Literal

from usan_api.compat.errors import CompatError
from usan_api.repositories import agent_profiles as agent_profiles_repo
```

(merging with the existing import block — `Any`, `Query`, `Depends`, `agent_bridge`, `ids` are already there), and append the handler:

```python
@router.get("/v2/list-retell-llms")
async def list_retell_llms_v2(
    request: Request,
    sort_order: Literal["ascending", "descending"] = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    """GET /v2/list-retell-llms — paginated ``{items, has_more[, pagination_key]}``.

    Keyset pagination over (created_at, id), a structural port of v2/list-conversation-flows.
    The unversioned root /list-retell-llms (bare array) is a separate frozen op.
    ``pagination_key`` is emitted only when has_more (RetellAI omit-nulls). Channel-agnostic:
    chat-bound LLMs appear alongside voice ones.
    """
    after = None
    if pagination_key:
        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after = ids.decode_retell_llm_cursor(pagination_key)
    profiles = await agent_profiles_repo.list_profiles_keyset(
        db, limit=limit, descending=(sort_order != "ascending"), after=after
    )
    _audit(request, "list-retell-llms-v2")
    has_more = len(profiles) > limit
    page = profiles[:limit]
    out: dict[str, Any] = {
        "items": [agent_bridge.serialize_llm(p).model_dump() for p in page],
        "has_more": has_more,
    }
    if has_more:
        out["pagination_key"] = ids.encode_retell_llm_cursor(page[-1].created_at, page[-1].id)
    return out
```

Item serialization is deliberately identical to the root list (`serialize_llm(p).model_dump()`), so both list views stay in lockstep.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compat/test_list_retell_llms_v2.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Surface + frozen-agents regression check**

Run: `uv run pytest tests/compat/test_surface_coverage.py tests/compat/test_freeze_agents.py tests/test_compat_fidelity.py tests/compat/test_chat_agent_isolation.py -v`
Expected: ALL PASS (root bare-array list untouched; v2 now served).

- [ ] **Step 6: mypy + ruff, then commit**

```bash
uv run mypy && uv run ruff check . && uv run ruff format .
git add src/usan_api/compat/routers/retell_llm.py src/usan_api/compat/routers/unsupported.py tests/compat/test_list_retell_llms_v2.py
git commit -m "feat(api): GET /v2/list-retell-llms — 501 -> served (keyset-paginated)"
```

---

### Task 7: Full gate + surface verification

**Files:**
- No new files; verification + possible test-drift fixes only.

- [ ] **Step 1: Confirm no stale 501 references to the two paths**

Run: `grep -rn "list-retell-llms\|rerun-call-analysis" tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py src/usan_api/compat/routers/unsupported.py`
Expected: NO matches in any of the three files (unsupported.py entries removed in Tasks 4/6; neither coverage file ever referenced them).

- [ ] **Step 2: Alembic single head**

Run: `uv run alembic heads`
Expected: exactly one head, `0051`.

- [ ] **Step 3: Full API suite**

Run: `uv run pytest`
Expected: ALL PASS (parallel `-n auto` is the default; do not run a second suite concurrently — testcontainer startup flakes under concurrent runs). If an unrelated test flakes, re-run it in isolation before assuming a regression.

- [ ] **Step 4: Lint + types**

Run: `uv run mypy && uv run ruff check . && uv run ruff format .`
Expected: clean.

- [ ] **Step 5: Commit any drift fixes**

```bash
git add -A && git diff --cached --quiet || git commit -m "test(api): phase 7 slice 2 gate fixes"
```
