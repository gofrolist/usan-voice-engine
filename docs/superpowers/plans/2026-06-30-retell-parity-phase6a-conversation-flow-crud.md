# RetellAI Parity Phase 6a — Conversation-Flow CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the 5 RetellAI conversation-flow CRUD ops (create/get/update/delete/list) at exact oracle paths + shapes, persisting the flow DAG as opaque JSONB and echoing it conformantly — never executing it (persisted-not-honored).

**Architecture:** A standalone per-org entity mirroring the Phase-2 phone-number build (dedicated table + repo + schema + router; CRUD lives directly in the router, no service layer). New `conversation_flows` table (migration 0048, FORCE-RLS), new `conversation_flow_` id-prefix, new `compat/routers/conversation_flow.py` mounted in the compat app. Zero edits to agent/LLM/worker code.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Pydantic v2, Alembic, Postgres (RLS), pytest. apps/api only (Python 3.14, uv).

**Spec:** `docs/superpowers/specs/2026-06-30-retell-parity-phase6a-conversation-flow-crud-design.md` (committed 6233f12).

## Global Constraints

Every task implicitly includes these (copied from the spec §10, with two corrections/refinements noted):

- **apps/api only** — `services/agent` untouched; no cross-service import.
- **Single new alembic migration `0048`** (`down_revision = "0047"`); single head after merge; owner-DDL (CREATE TABLE + GRANT + RLS run as the `usan` owner on deploy — like 0046/0047).
- Ships **INERT** — no behavior change until a `v*` tag; no flag needed (compat surface is key-gated; the flow is never executed in 6a).
- **`KNOWN_GAPS` stays `frozenset()`**; both surface-coverage files (`tests/compat/test_surface_coverage.py`, `tests/test_compat_fidelity.py`) stay consistent; no new served op beyond the 5 flow ops — the 5 **component** stubs stay 501.
- **Error envelope is `{"status": <int>, "message": <str>}`** (the actual `compat/errors.py` shape — int status, NOT `{"status":"error",…}`; the spec §7.1 wording is superseded by this). 404 body = `{"status": 404, "message": "conversation flow not found"}`; 422 via the global `RequestValidationError` handler = `{"status": 422, "message": "invalid request: <field>"}`.
- **No service module** (refinement from spec §5.3): CRUD lives in the router (the phone_numbers pattern); `serialize_flow` lives in `compat/schemas/conversation_flow.py` (mirrors `serialize_phone_number`).
- Persist with **null-dropped** body (`{k: v for k, v in body.model_dump().items() if v is not None}`) and echo only stored fields → oracle omit-nulls holds without an explicit response `exclude_none`.
- **CI mypy = `uv run mypy`** (config `files=["src"]`) — never `mypy .`. **ruff** line-length 100, target py314.
- This env's text display strips parens from `except (A, B):` → verify any such line via `python -m py_compile`, not by eye.
- **pytest `-n auto`** (parallel) — tests tolerate sibling rows (scope assertions to ids you created); **RLS-meaningful asserts run on the non-superuser `app_session`** (CI `usan` superuser bypasses RLS).
- Compat session does **not** autocommit — the router commits explicitly after each mutation.
- Commit format `type(scope): description`, scope `api`/`docs`. Attribution disabled (no `Co-Authored-By`, no footer).
- Squash-merge to protected `main` ONLY on explicit go-ahead; **no `v*` tag**.

---

## File Structure

| File | New/Edit | Responsibility |
|---|---|---|
| `apps/api/migrations/versions/0048_conversation_flows.py` | New | The `conversation_flows` table (FORCE-RLS, owner-DDL). |
| `apps/api/src/usan_api/db/models.py` | Edit (additive) | `ConversationFlow(Base, TenantScoped)` ORM model. |
| `apps/api/src/usan_api/compat/ids.py` | Edit (additive) | `conversation_flow_` id prefix + encode/decode + keyset cursor. |
| `apps/api/src/usan_api/repositories/conversation_flows.py` | New | `create / get / update / archive / list_flows` (flush-only). |
| `apps/api/src/usan_api/compat/schemas/conversation_flow.py` | New | Request models + `serialize_flow`. |
| `apps/api/src/usan_api/compat/routers/conversation_flow.py` | New | The 5 routes (commit discipline, cursor). |
| `apps/api/src/usan_api/compat/app.py` | Edit | Mount the new router. |
| `apps/api/src/usan_api/compat/routers/unsupported.py` | Edit | Remove the 5 flow stubs (keep the 5 component stubs). |
| `apps/api/tests/test_compat_fidelity.py` | Edit | Swap the 2 `/create-conversation-flow` 501 references to a still-stubbed component path. |
| `apps/api/tests/test_conversation_flows_migration.py` | New (Task 1) | FORCE-RLS + grant assertions. |
| `apps/api/tests/test_conversation_flows_repo.py` | New (Task 2) | CRUD/keyset/cross-org. |
| `apps/api/tests/test_conversation_flow_schemas.py` | New (Task 3) | Request validation + serializer. |
| `apps/api/tests/compat/test_conversation_flow_crud.py` | New (Task 4) | HTTP CRUD behavior. |
| `apps/api/tests/compat/test_freeze_conversation_flows.py` | New (Task 5) | Frozen conformance (oracle + SDK). |
| `apps/api/docs/deployment/conversation-flows.md` | New (Task 6) | Operator note / posture. |

> All `cd apps/api` first. Tests: `uv run pytest -n0 <path>::<name> -v` for a single test (serial), `uv run pytest <path>` for a file. Full gate before the final review: `uv run pytest && uv run mypy && ruff check . && ruff format --check .`

---

## Task 1: Migration 0048 + `ConversationFlow` ORM model

**Files:**
- Create: `apps/api/migrations/versions/0048_conversation_flows.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add the model near the other compat tables, e.g. after `KnowledgeBase`)
- Test: `apps/api/tests/test_conversation_flows_migration.py`

**Interfaces:**
- Produces: a `conversation_flows` table and `usan_api.db.models.ConversationFlow` with columns `id: uuid`, `organization_id: uuid` (from `TenantScoped`), `config: dict`, `version: int`, `archived_at: datetime|None`, `created_at: datetime`, `updated_at: datetime`.

- [ ] **Step 1: Write the failing migration test**

`apps/api/tests/test_conversation_flows_migration.py` (mirrors `tests/test_knowledge_bases_migration.py`, but asserts **FORCE** = True — the opposite of the KB ENABLE-only exception, because `conversation_flows` is a plain per-org table with no cross-org SECURITY DEFINER accessor):

```python
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_conversation_flows_table_force_rls_and_grant(async_database_url: str) -> None:
    async def _check() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                relrowsecurity, relforcerowsecurity = (
                    await conn.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = 'conversation_flows'"
                        )
                    )
                ).one()
                # Plain per-org table -> FORCE (owner is also policy-bound). This is the
                # OPPOSITE of the 0047 KB tables (ENABLE-only for the cross-org claim fn).
                assert relrowsecurity is True
                assert relforcerowsecurity is True
                policy = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_policy "
                        "WHERE polrelid = 'conversation_flows'::regclass "
                        "AND polname = 'tenant_isolation'"
                    )
                )
                assert policy == 1
                grant = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.role_table_grants "
                        "WHERE table_name = 'conversation_flows' AND grantee = 'usan_app' "
                        "AND privilege_type = 'INSERT'"
                    )
                )
                assert grant == 1
        finally:
            await engine.dispose()

    asyncio.run(_check())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flows_migration.py -v`
Expected: FAIL (no `conversation_flows` table → `.one()` raises / returns no row).

- [ ] **Step 3: Write the migration**

`apps/api/migrations/versions/0048_conversation_flows.py` (mirrors `0046_chat_analyses.py`, FORCE variant; no FK to chat_sessions, no unique constraint):

```python
"""conversation_flows: TenantScoped + FORCE RLS table for RetellAI conversation-flow CRUD (6a).

New owner-DDL table (modeled on 0046). Plain per-org table — no cross-org accessor — so it
uses FORCE RLS (NOT the 0047 KB ENABLE-only exception). Stores the persisted-not-honored flow
body as JSONB. GRANT to usan_app so the least-priv runtime role can CRUD it. Additive + inert.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: str | None = "0047"
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
        "conversation_flows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_flows_organization_id", "conversation_flows", ["organization_id"]
    )
    _enable_rls("conversation_flows")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_flows")
    op.drop_index("ix_conversation_flows_organization_id", table_name="conversation_flows")
    op.drop_table("conversation_flows")
```

- [ ] **Step 4: Add the ORM model**

In `apps/api/src/usan_api/db/models.py`, add (all imports already present — `PhoneNumber` uses `UUID`, `JSONB`, `Integer`, `DateTime`, `func`, `text`, `Mapped`, `mapped_column`, `datetime`, `Any`):

```python
class ConversationFlow(Base, TenantScoped):
    """RetellAI conversation-flow (Phase 6a): the flow DAG persisted as opaque JSONB and echoed
    conformantly, but NOT executed at call/chat time (persisted-not-honored). Referenced by
    agents via response_engine.conversation_flow_id (the binding lands in 6c)."""

    __tablename__ = "conversation_flows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 5: Run the migration test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flows_migration.py -v`
Expected: PASS (the test harness migrates the test DB to head 0048; FORCE + policy + grant present). If the harness caches the schema, recreate the test DB per the project's usual flow.

- [ ] **Step 6: mypy + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format --check .`
```bash
git add apps/api/migrations/versions/0048_conversation_flows.py apps/api/src/usan_api/db/models.py apps/api/tests/test_conversation_flows_migration.py
git commit -m "feat(api): conversation_flows table + ORM model (Phase 6a, mig 0048 FORCE-RLS)"
```

---

## Task 2: id-codec prefix + cursor + repository

**Files:**
- Modify: `apps/api/src/usan_api/compat/ids.py` (additive)
- Create: `apps/api/src/usan_api/repositories/conversation_flows.py`
- Test: `apps/api/tests/test_conversation_flows_repo.py`

**Interfaces:**
- Consumes: `usan_api.db.models.ConversationFlow` (Task 1).
- Produces:
  - `ids.encode_conversation_flow_id(uuid) -> str` (`"conversation_flow_" + hex`); `ids.decode_conversation_flow_id(str) -> uuid` (CompatError(422) on malformed).
  - `ids.encode_conversation_flow_cursor(created_at: datetime, fid: uuid) -> str`; `ids.decode_conversation_flow_cursor(str) -> tuple[datetime, uuid]` (CompatError(422) on bad cursor).
  - repo `conversation_flows` module: `create(db, *, config, version=0) -> ConversationFlow`; `get(db, flow_id) -> ConversationFlow | None` (archived excluded); `update(db, flow_id, *, config, version) -> ConversationFlow | None`; `archive(db, flow_id) -> bool`; `list_flows(db, *, limit, descending, after) -> list[ConversationFlow]`.

- [ ] **Step 1: Write the failing repo + codec test**

`apps/api/tests/test_conversation_flows_repo.py` (mirrors `tests/test_phone_numbers_repo.py`; adds an archive + cross-org isolation check on `app_session`):

```python
from __future__ import annotations

import uuid

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.db.models import ConversationFlow
from usan_api.repositories import conversation_flows as repo
from usan_api.tenant_context import set_tenant_context


def test_id_codec_roundtrip_and_malformed() -> None:
    fid = uuid.uuid4()
    token = ids.encode_conversation_flow_id(fid)
    assert token == "conversation_flow_" + fid.hex
    assert ids.decode_conversation_flow_id(token) == fid
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_id("llm_" + fid.hex)  # wrong prefix
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_id("conversation_flow_zzzz")  # bad hex


@pytest.mark.asyncio
async def test_crud_keyset_archive(two_orgs, app_session) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create(app_session, config={"start_speaker": "agent", "nodes": []})
    b = await repo.create(app_session, config={"start_speaker": "user", "nodes": []})
    assert isinstance(a, ConversationFlow)
    assert a.version == 0

    got = await repo.get(app_session, a.id)
    assert got is not None
    assert got.config["start_speaker"] == "agent"
    assert await repo.get(app_session, uuid.uuid4()) is None

    upd = await repo.update(app_session, a.id, config={"start_speaker": "agent", "global_prompt": "hi"}, version=1)
    assert upd is not None
    assert upd.version == 1
    assert upd.config == {"start_speaker": "agent", "global_prompt": "hi"}

    page = await repo.list_flows(app_session, limit=10, descending=True, after=None)
    assert {f.id for f in page} >= {a.id, b.id}

    newest = page[0]
    after = await repo.list_flows(
        app_session, limit=10, descending=True, after=(newest.created_at, newest.id)
    )
    assert newest.id not in {f.id for f in after}

    assert await repo.archive(app_session, a.id) is True
    assert await repo.get(app_session, a.id) is None  # archived -> excluded
    assert await repo.archive(app_session, a.id) is False  # already gone


@pytest.mark.asyncio
async def test_cross_org_isolation(two_orgs, app_session) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    a = await repo.create(app_session, config={"nodes": []})
    flow_id = a.id
    # Switch to org B (non-superuser app_session is RLS-bound) -> A's flow is invisible.
    await set_tenant_context(app_session, org_b)
    assert await repo.get(app_session, flow_id) is None
    assert flow_id not in {f.id for f in await repo.list_flows(app_session, limit=100, descending=True, after=None)}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flows_repo.py -v`
Expected: FAIL (`ids.encode_conversation_flow_id` / `repo` not defined).

- [ ] **Step 3: Add the id codec**

In `apps/api/src/usan_api/compat/ids.py`, add the prefix constant beside the others and the four functions (the cursor pair is an intentional per-entity mirror of `encode/decode_phone_number_cursor` — a cross-entity cursor DRY is out of 6a scope, it would touch the frozen phone surface):

```python
_CONVERSATION_FLOW_PREFIX = "conversation_flow_"


def encode_conversation_flow_id(flow_id: uuid.UUID) -> str:
    return _CONVERSATION_FLOW_PREFIX + flow_id.hex


def decode_conversation_flow_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_CONVERSATION_FLOW_PREFIX, kind="conversation_flow_id")


def encode_conversation_flow_cursor(created_at: datetime, fid: uuid.UUID) -> str:
    """Opaque (created_at, id) keyset cursor (mirror of encode_phone_number_cursor)."""
    raw = f"{created_at.isoformat()}|{fid.hex}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_conversation_flow_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    try:
        padding = 4 - len(token) % 4
        padded = token + "=" * (padding % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        ts_part, hex_part = raw.split("|", 1)
        created_at = datetime.fromisoformat(ts_part)
        fid = uuid.UUID(hex=hex_part)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CompatError(422, "invalid pagination_key") from exc
    return created_at, fid
```

- [ ] **Step 4: Write the repository**

`apps/api/src/usan_api/repositories/conversation_flows.py` (mirrors `repositories/phone_numbers.py`; `get`/`list_flows` exclude archived rows):

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationFlow


async def create(db: AsyncSession, *, config: dict[str, Any], version: int = 0) -> ConversationFlow:
    cf = ConversationFlow(config=config, version=version)
    db.add(cf)
    await db.flush()
    await db.refresh(cf)
    return cf


async def get(db: AsyncSession, flow_id: uuid.UUID) -> ConversationFlow | None:
    result = await db.execute(
        select(ConversationFlow).where(
            ConversationFlow.id == flow_id, ConversationFlow.archived_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def update(
    db: AsyncSession, flow_id: uuid.UUID, *, config: dict[str, Any], version: int
) -> ConversationFlow | None:
    cf = await get(db, flow_id)
    if cf is None:
        return None
    cf.config = config
    cf.version = version
    await db.flush()
    await db.refresh(cf)
    return cf


async def archive(db: AsyncSession, flow_id: uuid.UUID) -> bool:
    cf = await get(db, flow_id)
    if cf is None:
        return False
    cf.archived_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_flows(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[ConversationFlow]:
    """Keyset-paginate the org's non-archived flows over (created_at, id). RLS scopes to the
    caller's org. Fetches limit+1 so the caller computes has_more without a COUNT."""
    stmt = select(ConversationFlow).where(ConversationFlow.archived_at.is_(None))
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    ConversationFlow.created_at < after_created_at,
                    and_(
                        ConversationFlow.created_at == after_created_at,
                        ConversationFlow.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    ConversationFlow.created_at > after_created_at,
                    and_(
                        ConversationFlow.created_at == after_created_at,
                        ConversationFlow.id > after_id,
                    ),
                )
            )
    if descending:
        stmt = stmt.order_by(ConversationFlow.created_at.desc(), ConversationFlow.id.desc())
    else:
        stmt = stmt.order_by(ConversationFlow.created_at.asc(), ConversationFlow.id.asc())
    stmt = stmt.limit(limit + 1)
    return list((await db.execute(stmt)).scalars().all())
```

- [ ] **Step 5: Run the repo test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flows_repo.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 6: mypy + commit**

```bash
cd apps/api && uv run mypy && ruff check . && ruff format --check .
git add apps/api/src/usan_api/compat/ids.py apps/api/src/usan_api/repositories/conversation_flows.py apps/api/tests/test_conversation_flows_repo.py
git commit -m "feat(api): conversation-flow id codec + repository (Phase 6a)"
```

---

## Task 3: Request schemas + serializer

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/conversation_flow.py`
- Test: `apps/api/tests/test_conversation_flow_schemas.py`

**Interfaces:**
- Consumes: `ids.encode_conversation_flow_id` (Task 2), `ConversationFlow` (Task 1).
- Produces:
  - `CreateConversationFlowRequest` (Pydantic, `extra="allow"`, required `start_speaker: str`, `model_choice: dict`, `nodes: list`).
  - `UpdateConversationFlowRequest` (Pydantic, `extra="allow"`, no declared fields).
  - `serialize_flow(row: ConversationFlow) -> dict[str, Any]` (echo config + 3 server fields).

- [ ] **Step 1: Write the failing schema test**

`apps/api/tests/test_conversation_flow_schemas.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.conversation_flow import (
    CreateConversationFlowRequest,
    UpdateConversationFlowRequest,
    serialize_flow,
)
from usan_api.db.models import ConversationFlow

_MODEL = {"type": "cascading", "model": "gpt-4.1"}


def test_create_request_requires_three_fields() -> None:
    with pytest.raises(ValidationError):
        CreateConversationFlowRequest(model_choice=_MODEL, nodes=[])  # missing start_speaker
    with pytest.raises(ValidationError):
        CreateConversationFlowRequest(start_speaker="agent", nodes=[])  # missing model_choice


def test_create_request_captures_extras() -> None:
    body = CreateConversationFlowRequest(
        start_speaker="agent",
        model_choice=_MODEL,
        nodes=[{"id": "n1", "type": "conversation"}],
        global_prompt="hi",
        tools=[{"type": "end_call"}],
    )
    dumped = body.model_dump()
    assert dumped["start_speaker"] == "agent"
    assert dumped["global_prompt"] == "hi"
    assert dumped["tools"] == [{"type": "end_call"}]
    assert dumped["nodes"] == [{"id": "n1", "type": "conversation"}]


def test_update_request_is_opaque_partial() -> None:
    body = UpdateConversationFlowRequest(global_prompt="b")
    assert body.model_dump() == {"global_prompt": "b"}


def test_serialize_flow_echoes_config_and_server_fields() -> None:
    fid = uuid.uuid4()
    row = ConversationFlow(config={"start_speaker": "agent", "global_prompt": "hi"}, version=2)
    row.id = fid
    row.updated_at = datetime(2026, 6, 30, tzinfo=UTC)
    out = serialize_flow(row)
    assert out["conversation_flow_id"] == "conversation_flow_" + fid.hex
    assert out["version"] == 2
    assert out["start_speaker"] == "agent"
    assert out["global_prompt"] == "hi"
    assert out["last_modification_timestamp"] == int(datetime(2026, 6, 30, tzinfo=UTC).timestamp() * 1000)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flow_schemas.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the schemas + serializer**

`apps/api/src/usan_api/compat/schemas/conversation_flow.py`:

```python
"""RetellAI-compat conversation-flow request/response schemas + serializer (Phase 6a).

The flow body is captured opaquely (extra='allow'): only the 3 oracle-required create fields
are presence-checked; nodes/tools/components/mcps and every other field ride through unvalidated
and are persisted/echoed verbatim (persisted-not-honored — semantic validation is the runtime's
job). serialize_flow echoes the stored body + the 3 server-generated fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat import ids
from usan_api.db.models import ConversationFlow


class CreateConversationFlowRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    start_speaker: str
    model_choice: dict[str, Any]
    nodes: list[Any]


class UpdateConversationFlowRequest(BaseModel):
    """Oracle ConversationFlow: every field optional. Opaque — any subset of top-level fields
    is accepted and shallow-merged over the stored config by the router."""

    model_config = ConfigDict(extra="allow")


def serialize_flow(row: ConversationFlow) -> dict[str, Any]:
    data: dict[str, Any] = dict(row.config)
    data["conversation_flow_id"] = ids.encode_conversation_flow_id(row.id)
    data["version"] = row.version
    data["last_modification_timestamp"] = int(row.updated_at.timestamp() * 1000)
    return data
```

- [ ] **Step 4: Run the schema test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_conversation_flow_schemas.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: mypy + commit**

```bash
cd apps/api && uv run mypy && ruff check . && ruff format --check .
git add apps/api/src/usan_api/compat/schemas/conversation_flow.py apps/api/tests/test_conversation_flow_schemas.py
git commit -m "feat(api): conversation-flow request schemas + serializer (Phase 6a)"
```

---

## Task 4: Router (5 endpoints) + mount + stub removal + fidelity-test edits

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/conversation_flow.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (mount)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the 5 flow stubs)
- Modify: `apps/api/tests/test_compat_fidelity.py` (swap the 2 `/create-conversation-flow` references)
- Test: `apps/api/tests/compat/test_conversation_flow_crud.py`

**Interfaces:**
- Consumes: `flows_repo` (Task 2), the schemas + `serialize_flow` (Task 3), `ids` (Task 2), `get_compat_db`, `CompatError`.
- Produces: the 5 mounted routes (`/create-conversation-flow`, `/get-conversation-flow/{id}`, `/update-conversation-flow/{id}`, `/delete-conversation-flow/{id}`, `/v2/list-conversation-flows`).

- [ ] **Step 1: Write the failing HTTP CRUD test**

`apps/api/tests/compat/test_conversation_flow_crud.py` (mirrors the phone CRUD/list tests; uses `compat_client` + `compat_headers`; no agent needed — a flow is standalone):

```python
from __future__ import annotations

_FLOW = {"start_speaker": "agent", "model_choice": {"type": "cascading", "model": "gpt-4.1"}, "nodes": []}


def _create(compat_client, compat_headers, **extra) -> dict:
    r = compat_client.post(
        "/create-conversation-flow", json={**_FLOW, **extra}, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers, global_prompt="hi")
    cid = body["conversation_flow_id"]
    assert cid.startswith("conversation_flow_")
    assert body["version"] == 0
    assert body["start_speaker"] == "agent"
    assert body["global_prompt"] == "hi"
    g = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["conversation_flow_id"] == cid


def test_update_merges_top_level_and_bumps_version(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, global_prompt="a")["conversation_flow_id"]
    u1 = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": "b"}, headers=compat_headers
    )
    assert u1.status_code == 200, u1.text
    assert u1.json()["version"] == 1
    assert u1.json()["global_prompt"] == "b"
    # Omitting global_prompt preserves it; a new top-level field is added; version bumps again.
    u2 = compat_client.patch(
        f"/update-conversation-flow/{cid}",
        json={"model_temperature": 0.5},
        headers=compat_headers,
    )
    assert u2.status_code == 200
    body = u2.json()
    assert body["version"] == 2
    assert body["global_prompt"] == "b"  # preserved
    assert body["model_temperature"] == 0.5


def test_version_query_param_is_accepted_and_ignored(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_id"]
    g = compat_client.get(f"/get-conversation-flow/{cid}?version=7", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["version"] == 0  # current, not 7


def test_delete_then_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_id"]
    d = compat_client.delete(f"/delete-conversation-flow/{cid}", headers=compat_headers)
    assert d.status_code == 204
    assert d.content == b""
    assert compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers).status_code == 404


def test_malformed_id_is_422_and_missing_is_404(compat_client, compat_headers) -> None:
    import uuid

    assert compat_client.get("/get-conversation-flow/not_a_flow_id", headers=compat_headers).status_code == 422
    missing = "conversation_flow_" + uuid.uuid4().hex
    assert compat_client.get(f"/get-conversation-flow/{missing}", headers=compat_headers).status_code == 404


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    created = {_create(compat_client, compat_headers)["conversation_flow_id"] for _ in range(3)}
    r = compat_client.get("/v2/list-conversation-flows?limit=2", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    # Walk pages until the 3 we created are all seen (siblings from -n auto are tolerated).
    seen = {i["conversation_flow_id"] for i in body["items"]}
    key = body["pagination_key"]
    for _ in range(20):
        nxt = compat_client.get(
            f"/v2/list-conversation-flows?limit=2&pagination_key={key}", headers=compat_headers
        ).json()
        seen |= {i["conversation_flow_id"] for i in nxt["items"]}
        if not nxt.get("has_more"):
            break
        key = nxt["pagination_key"]
    assert created <= seen
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_conversation_flow_crud.py -v`
Expected: FAIL (POST `/create-conversation-flow` returns **501** — the stub still mounted).

- [ ] **Step 3: Write the router**

`apps/api/src/usan_api/compat/routers/conversation_flow.py`:

```python
"""RetellAI-compat conversation-flow CRUD (Phase 6a): create/get/update/delete/list.

The flow DAG is persisted (JSONB) and echoed conformantly but NOT executed at call/chat time
(persisted-not-honored — the DAG runtime is a later sub-phase). A flow is a standalone entity
(its own conversation_flows table), referenced by agents in 6c. The session does not autocommit;
each mutation commits explicitly. See docs/deployment/conversation-flows.md.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.conversation_flow import (
    CreateConversationFlowRequest,
    UpdateConversationFlowRequest,
    serialize_flow,
)
from usan_api.repositories import conversation_flows as flows_repo

router = APIRouter(tags=["compat-conversation-flows"])


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the flow config (it can carry prompts).
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat conversation-flow op={op}")


def _provided(model: CreateConversationFlowRequest | UpdateConversationFlowRequest) -> dict[str, Any]:
    # Drop null-valued keys (declared and extra='allow') so we store/merge only real values.
    return {k: v for k, v in model.model_dump().items() if v is not None}


@router.post("/create-conversation-flow", status_code=status.HTTP_201_CREATED)
async def create_conversation_flow(
    body: CreateConversationFlowRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    row = await flows_repo.create(db, config=_provided(body), version=0)
    await db.commit()
    _audit(request, "create-conversation-flow")
    return serialize_flow(row)


@router.get("/get-conversation-flow/{conversation_flow_id}")
async def get_conversation_flow(
    conversation_flow_id: str,
    request: Request,
    version: int | None = Query(default=None),  # accepted, ignored (current-only)
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    row = await flows_repo.get(db, flow_id)
    if row is None:
        raise CompatError(404, "conversation flow not found")
    _audit(request, "get-conversation-flow")
    return serialize_flow(row)


@router.patch("/update-conversation-flow/{conversation_flow_id}")
async def update_conversation_flow(
    conversation_flow_id: str,
    body: UpdateConversationFlowRequest,
    request: Request,
    version: int | None = Query(default=None),  # accepted, ignored
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    row = await flows_repo.get(db, flow_id)
    if row is None:
        raise CompatError(404, "conversation flow not found")
    merged = {**row.config, **_provided(body)}  # top-level shallow merge
    updated = await flows_repo.update(db, flow_id, config=merged, version=row.version + 1)
    assert updated is not None  # loaded above in the same txn
    await db.commit()
    _audit(request, "update-conversation-flow")
    return serialize_flow(updated)


@router.delete(
    "/delete-conversation-flow/{conversation_flow_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation_flow(
    conversation_flow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    flow_id = ids.decode_conversation_flow_id(conversation_flow_id)
    if not await flows_repo.archive(db, flow_id):
        raise CompatError(404, "conversation flow not found")
    await db.commit()
    _audit(request, "delete-conversation-flow")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v2/list-conversation-flows")
async def list_conversation_flows(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after = None
    if pagination_key:
        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after = ids.decode_conversation_flow_cursor(pagination_key)
    rows = await flows_repo.list_flows(
        db, limit=limit, descending=(sort_order != "ascending"), after=after
    )
    _audit(request, "list-conversation-flows")
    has_more = len(rows) > limit
    page = rows[:limit]
    out: dict[str, Any] = {"items": [serialize_flow(r) for r in page], "has_more": has_more}
    if has_more:
        out["pagination_key"] = ids.encode_conversation_flow_cursor(page[-1].created_at, page[-1].id)
    return out
```

- [ ] **Step 4: Mount the router**

In `apps/api/src/usan_api/compat/app.py`, add the import (with the other `from usan_api.compat.routers import …` lines) and the `include_router` call (next to `compat_knowledge_bases`):

```python
from usan_api.compat.routers import conversation_flow as compat_conversation_flow
```
```python
    app.include_router(compat_conversation_flow.router)
```

- [ ] **Step 5: Remove the 5 flow stubs**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete the 5 conversation-flow entries from `_UNSUPPORTED` (the block under `# --- Conversation flow ---`, currently lines 26-31). **Keep** the 5 `# --- Conversation flow component ---` entries (currently lines 32-37). Result: the `_UNSUPPORTED` tuple no longer contains any `/…-conversation-flow` path, but retains all `/…-conversation-flow-component` paths.

- [ ] **Step 6: Fix the fidelity test references**

In `apps/api/tests/test_compat_fidelity.py`:
- In the `test_out_of_scope_returns_501_envelope` parametrize list, change `("post", "/create-conversation-flow")` → `("post", "/create-conversation-flow-component")` (a still-stubbed component op).
- In `test_unsupported_still_requires_key`, change `compat_client.post("/create-conversation-flow", json={})` → `compat_client.post("/create-conversation-flow-component", json={})`.

- [ ] **Step 7: Grep for any other 501 assertion on flow paths**

Run: `cd apps/api && grep -rn "conversation-flow" tests/ | grep -v "conversation-flow-component" | grep -iE "501|unsupported|not_supported"`
Expected: no remaining test asserts a `/…-conversation-flow` path returns 501. (If any surfaces, update it to a served-route assertion.)

- [ ] **Step 8: Run the CRUD + surface-coverage + fidelity tests**

Run:
```bash
cd apps/api && uv run pytest -n0 \
  tests/compat/test_conversation_flow_crud.py \
  tests/compat/test_surface_coverage.py \
  tests/test_compat_fidelity.py -v
```
Expected: all PASS (CRUD works; `KNOWN_GAPS` still `frozenset()`; the 5 component ops still 501; the swapped fidelity cases pass).

- [ ] **Step 9: mypy + commit**

```bash
cd apps/api && uv run mypy && ruff check . && ruff format --check .
git add apps/api/src/usan_api/compat/routers/conversation_flow.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/test_compat_fidelity.py apps/api/tests/compat/test_conversation_flow_crud.py
git commit -m "feat(api): conversation-flow CRUD router + mount; promote 5 stubs to served (Phase 6a)"
```

---

## Task 5: Frozen conformance test

**Files:**
- Test: `apps/api/tests/compat/test_freeze_conversation_flows.py`

**Interfaces:**
- Consumes: the served routes (Task 4); `assert_conforms` / `assert_sdk_roundtrip` from `tests.compat.conformance`.

- [ ] **Step 1: Write the frozen conformance test**

`apps/api/tests/compat/test_freeze_conversation_flows.py` (the conformance fixture uses `nodes: []` — an empty node array is oracle-valid and dodges per-node-type required-field fragility; `model_choice.model` `"gpt-4.1"` is in the oracle `LLMModel` enum):

```python
"""Frozen conformance for the compat conversation-flow surface (Phase 6a)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gpt-4.1"},
    "nodes": [],
    "global_prompt": "You are a helpful agent.",
}


def test_create_conforms(compat_client, compat_headers) -> None:
    r = compat_client.post("/create-conversation-flow", json=_FLOW, headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["conversation_flow_id"].startswith("conversation_flow_")
    assert isinstance(body["version"], int)
    assert isinstance(body["last_modification_timestamp"], int)
    assert_conforms(body, "ConversationFlowResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowResponse")


def test_get_update_list_conform(compat_client, compat_headers) -> None:
    cid = compat_client.post(
        "/create-conversation-flow", json=_FLOW, headers=compat_headers
    ).json()["conversation_flow_id"]

    g = compat_client.get(f"/get-conversation-flow/{cid}", headers=compat_headers)
    assert g.status_code == 200
    assert_conforms(g.json(), "ConversationFlowResponse")

    u = compat_client.patch(
        f"/update-conversation-flow/{cid}", json={"global_prompt": "v2"}, headers=compat_headers
    )
    assert u.status_code == 200
    assert_conforms(u.json(), "ConversationFlowResponse")

    lst = compat_client.get("/v2/list-conversation-flows?limit=2", headers=compat_headers)
    assert lst.status_code == 200
    body = lst.json()
    assert isinstance(body["items"], list)
    for item in body["items"]:
        assert_conforms(item, "ConversationFlowResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowListResponse")
```

- [ ] **Step 2: Run it**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_conversation_flows.py -v`
Expected: PASS. (If `retell.types:ConversationFlowListResponse` fails to import — it should not; it is re-exported like `PhoneNumberListResponse` — confirm with `uv run python -c "import retell.types as t; print(hasattr(t,'ConversationFlowListResponse'))"` and, only if False, use `retell.types.conversation_flow_list_response:ConversationFlowListResponse`.)

- [ ] **Step 3: Commit**

```bash
cd apps/api && uv run mypy && ruff check . && ruff format --check .
git add apps/api/tests/compat/test_freeze_conversation_flows.py
git commit -m "test(api): freeze conversation-flow conformance (oracle + SDK) (Phase 6a)"
```

---

## Task 6: Operator documentation

**Files:**
- Create: `apps/api/docs/deployment/conversation-flows.md`

> Confirm the docs directory: prior phases wrote to `docs/deployment/*.md`. Check whether it is `apps/api/docs/deployment/` or the repo-root `docs/deployment/` (e.g. `ls docs/deployment/ apps/api/docs/deployment/ 2>/dev/null`) and place this file alongside the existing phase docs (`phone-numbers-bindings-deferred.md`, `knowledge-bases.md`). Use that same directory.

- [ ] **Step 1: Write the operator note**

Content (adapt the path to wherever the sibling phase docs live):

```markdown
# Conversation Flows (RetellAI parity, Phase 6a)

## What this is
The 5 conversation-flow CRUD ops (`create/get/update/delete-conversation-flow`,
`GET /v2/list-conversation-flows`) are served at the RetellAI oracle paths and shapes. A flow's
DAG is **persisted and echoed conformantly but NOT executed** at call/chat time
(persisted-not-honored). The DAG runtime, the 5 conversation-flow-**component** ops, and the
agent->flow binding are later sub-phases (6b / 6c / 6-runtime).

## Activation
Nothing to flip. The compat surface is gated only by a minted compat key; the new routes are
inert (a stored flow is never run). Migration `0048` (the `conversation_flows` table, FORCE-RLS)
ships with the next `v*` tag and is owner-DDL (runs as the `usan` owner via the deploy migration
path). No new env keys, no feature flag.

## Posture (documented deviations)
- **Accept-and-echo:** only `start_speaker`, `model_choice`, `nodes` are presence-checked on
  create (422 if absent). Node graphs, `model_choice.model` (RetellAI's OpenAI/Anthropic/Gemini
  enum - which we do not run), and edges are stored opaquely and never validated or interpreted.
- **Current-only versioning:** `version` starts at 0 and increments on each update;
  `?version` on get/update is accepted but always serves current (no version history).
- **Soft delete:** delete sets `archived_at`; get/list exclude archived rows.
- **RLS:** `conversation_flows` is per-org FORCE-RLS; isolation is enforced for the
  least-privilege `usan_app` runtime role.

## Security / PHI
Flow `config` may carry prompts -> it is NEVER logged (audit logs carry org + op only). The
RetellAI error envelope (`{"status": <int>, "message": ...}`) never echoes request bodies.

## Known limitation
Single-org persisted-not-honored CRUD. Binding a flow to an agent (`response_engine.type=
"conversation-flow"`) and executing it are deferred.
```

- [ ] **Step 2: Commit**

```bash
git add <path>/conversation-flows.md
git commit -m "docs(api): conversation-flow CRUD operator note (Phase 6a)"
```

---

## Final Gate (before requesting the whole-branch review)

Run the full suite + types + lint (parallel is fine here):
```bash
cd apps/api && uv run pytest && uv run mypy && ruff check . && ruff format --check .
```
Expected: green; single alembic head `0048` (`uv run alembic heads`); `KNOWN_GAPS` still `frozenset()`; `services/agent` untouched (`git diff --stat origin/main...HEAD` shows only `apps/api/**` + `docs/**`).

---

## Plan Self-Review

**1. Spec coverage** — every spec §2 in-scope item maps to a task: the 5 ops (Task 4) · table+RLS (Task 1) · id-codec (Task 2) · frozen conformance + stub removal + fidelity edits (Tasks 4-5) · posture doc (Task 6). Deferrals (components/binding/runtime) are untouched (component stubs stay in `_UNSUPPORTED`). Versioning = current-only (Task 4 `version+1`, `?version` ignored). The envelope shape was corrected to the real `{"status": <int>, …}` and the service-module was dropped in favor of the router-direct phone pattern — both noted in Global Constraints.

**2. Placeholder scan** — no TBD/TODO; every code step shows complete code; the one contingency (SDK list-response import) carries a concrete fallback path + a verification command, not a placeholder; the docs-dir confirmation is a real one-line check, not a deferral of content.

**3. Type consistency** — `serialize_flow` returns `dict[str, Any]` (router returns the dict, no response_model). `flows_repo` function names/signatures (`create/get/update/archive/list_flows`) are identical across Tasks 2-4. `ids.encode/decode_conversation_flow_id` + `…_cursor` names match across ids.py, repo, and router. `CreateConversationFlowRequest`/`UpdateConversationFlowRequest` names match across schema (Task 3) and router (Task 4). `_provided(...)` drops nulls for both create and update (consistent merge semantics).
