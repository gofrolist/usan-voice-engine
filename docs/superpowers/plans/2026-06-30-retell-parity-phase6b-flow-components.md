# Phase 6b — Conversation-Flow Components CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the five 501-stubbed `conversation-flow-component` endpoints to LIVE, persisted-not-honored, on a dedicated `conversation_flow_components` table — a structural port of the frozen Phase 6a flow surface.

**Architecture:** Own FORCE-RLS table (migration 0049), reversible id codec + keyset cursor, opaque request schemas (`extra="allow"`), a repository mirroring `conversation_flows`, and a router mirroring `conversation_flow.py` mounted in the compat sub-app. The component body is stored as opaque JSONB and echoed conformantly; it is not executed at call/chat time.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, pytest (`-n auto`), Postgres + RLS, retell-sdk 5.53.0 (conformance oracle).

**Design spec:** `docs/superpowers/specs/2026-06-30-retell-parity-phase6b-flow-components-design.md`

## Global Constraints

- **Python 3.14 (apps/api), type hints required; ruff line-length 100, target py314.** Run `ruff check . && ruff format .` and `uv run mypy` before every commit — CI runs both.
- **Run tests from `apps/api/`** with `uv run pytest` (parallel `-n auto` by default; use `-n0 ... -s` for a single serial test).
- **Server-generated fields** (`conversation_flow_component_id`, `user_modified_timestamp`) are always derived from ORM columns, stripped from stored config, never trusted from the client body.
- **PHI discipline:** never log the component config; audit logs carry `compat_org_id` + `op` only.
- **RLS:** the new table is FORCE row-level-security (plain per-org, like 0048 — NOT the 0047 KB ENABLE-only exception).
- **No `version`** anywhere — the oracle `ConversationFlowComponentResponse` has no version field (the one divergence from 6a).
- **Migrations run as the `usan` OWNER on deploy**, so owner-DDL + `GRANT ... TO usan_app` is correct; keep the migration additive + inert.
- **Single Alembic head** after this work: `0049`.

---

### Task 1: Migration 0049 + ORM model + migration test

**Files:**
- Create: `apps/api/migrations/versions/0049_conversation_flow_components.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add `ConversationFlowComponent`, after `ConversationFlow` ~line 1483)
- Test: `apps/api/tests/test_conversation_flow_components_migration.py`

**Interfaces:**
- Produces: table `conversation_flow_components(id, organization_id, config JSONB, archived_at, created_at, updated_at)` with FORCE RLS + `tenant_isolation` policy + 4 `usan_app` grants; ORM class `ConversationFlowComponent` with columns `id`, `config`, `archived_at`, `created_at`, `updated_at` (**no `version`**).

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0049_conversation_flow_components.py`:

```python
"""conversation_flow_components: TenantScoped + FORCE RLS table for RetellAI component CRUD (6b).

New owner-DDL table (modeled on 0048). Plain per-org table — no cross-org accessor — so it
uses FORCE RLS (NOT the 0047 KB ENABLE-only exception). Stores the persisted-not-honored
component body as JSONB. No version column: the oracle ConversationFlowComponentResponse has
none. GRANT to usan_app so the least-priv runtime role can CRUD it. Additive + inert.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
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
        "conversation_flow_components",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False),
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
        "ix_conversation_flow_components_organization_id",
        "conversation_flow_components",
        ["organization_id"],
    )
    _enable_rls("conversation_flow_components")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_flow_components")
    op.drop_index(
        "ix_conversation_flow_components_organization_id",
        table_name="conversation_flow_components",
    )
    op.drop_table("conversation_flow_components")
```

- [ ] **Step 2: Add the ORM model**

In `apps/api/src/usan_api/db/models.py`, immediately after the `ConversationFlow` class (ends ~line 1483), add:

```python
class ConversationFlowComponent(Base, TenantScoped):
    """RetellAI conversation-flow component (Phase 6b): a shared, reusable flow fragment persisted
    as opaque JSONB and echoed conformantly, but NOT executed at call/chat time
    (persisted-not-honored). Standalone entity; no version column (the oracle response has none)."""

    __tablename__ = "conversation_flow_components"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 3: Write the migration test**

Create `apps/api/tests/test_conversation_flow_components_migration.py`:

```python
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_conversation_flow_components_force_rls_and_grant(async_database_url: str) -> None:
    async def _check() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                relrowsecurity, relforcerowsecurity = (
                    await conn.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = 'conversation_flow_components'"
                        )
                    )
                ).one()
                # Plain per-org table -> FORCE (owner is also policy-bound), like 0048.
                assert relrowsecurity is True
                assert relforcerowsecurity is True
                policy = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_policy "
                        "WHERE polrelid = 'conversation_flow_components'::regclass "
                        "AND polname = 'tenant_isolation'"
                    )
                )
                assert policy == 1
                grant_count = await conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.role_table_grants "
                        "WHERE table_name = 'conversation_flow_components' "
                        "AND grantee = 'usan_app' "
                        "AND privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')"
                    )
                )
                assert grant_count == 4
        finally:
            await engine.dispose()

    asyncio.run(_check())
```

- [ ] **Step 4: Verify single head, run migration test**

Run:
```bash
cd apps/api && uv run alembic heads
```
Expected: a single head `0049 (head)`.

Run:
```bash
cd apps/api && uv run pytest tests/test_conversation_flow_components_migration.py -v
```
Expected: PASS (the test harness applies migrations to a fresh DB).

- [ ] **Step 5: Lint + typecheck**

Run:
```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/migrations/versions/0049_conversation_flow_components.py \
        apps/api/src/usan_api/db/models.py \
        apps/api/tests/test_conversation_flow_components_migration.py
git commit -m "feat(api): conversation_flow_components table + ORM model (Phase 6b, mig 0049 FORCE-RLS)"
```

---

### Task 2: ID codec + repository

**Files:**
- Modify: `apps/api/src/usan_api/compat/ids.py`
- Create: `apps/api/src/usan_api/repositories/conversation_flow_components.py`
- Test: `apps/api/tests/test_conversation_flow_components_repo.py`

**Interfaces:**
- Consumes: `ConversationFlowComponent` (Task 1); shared `_encode_keyset_cursor`/`_decode_keyset_cursor`, `_decode_hex` (existing in `ids.py`).
- Produces:
  - `ids.encode_conversation_flow_component_id(uuid.UUID) -> str`
  - `ids.decode_conversation_flow_component_id(str) -> uuid.UUID`
  - `ids.encode_conversation_flow_component_cursor(datetime, uuid.UUID) -> str`
  - `ids.decode_conversation_flow_component_cursor(str) -> tuple[datetime, uuid.UUID]`
  - repo funcs `create(db, *, config)`, `get(db, component_id)`, `update(db, component_id, *, config)`, `archive(db, component_id)`, `list_components(db, *, limit, descending, after)`.

- [ ] **Step 1: Write the repo + codec tests (failing)**

Create `apps/api/tests/test_conversation_flow_components_repo.py`:

```python
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.db.models import ConversationFlowComponent
from usan_api.repositories import conversation_flow_components as repo
from usan_api.tenant_context import set_tenant_context


def test_cursor_codec_roundtrip_and_bad_input() -> None:
    cid = uuid.uuid4()
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
    token = ids.encode_conversation_flow_component_cursor(now, cid)
    decoded_at, decoded_id = ids.decode_conversation_flow_component_cursor(token)
    assert decoded_id == cid
    assert decoded_at == now
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_cursor("not-valid-base64!!!")
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_cursor(
            base64.urlsafe_b64encode(b"no-pipe-separator").decode().rstrip("=")
        )


def test_id_codec_roundtrip_and_malformed() -> None:
    cid = uuid.uuid4()
    token = ids.encode_conversation_flow_component_id(cid)
    assert token == "conversation_flow_component_" + cid.hex
    assert ids.decode_conversation_flow_component_id(token) == cid
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id("llm_" + cid.hex)  # wrong prefix
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id("conversation_flow_component_zzzz")  # bad hex


def test_id_codec_rejects_flow_prefix_collision() -> None:
    # A bare conversation_flow_ id must NOT decode as a component id (prefix is a strict superset).
    fid = uuid.uuid4()
    flow_token = ids.encode_conversation_flow_id(fid)  # "conversation_flow_<hex>"
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id(flow_token)


@pytest.mark.asyncio
async def test_crud_keyset_archive(two_orgs, app_session) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create(app_session, config={"name": "Collector", "nodes": []})
    b = await repo.create(app_session, config={"name": "Router", "nodes": []})
    assert isinstance(a, ConversationFlowComponent)

    got = await repo.get(app_session, a.id)
    assert got is not None
    assert got.config["name"] == "Collector"
    assert await repo.get(app_session, uuid.uuid4()) is None

    upd = await repo.update(
        app_session, a.id, config={"name": "Collector", "flex_mode": True}
    )
    assert upd is not None
    assert upd.config == {"name": "Collector", "flex_mode": True}

    page = await repo.list_components(app_session, limit=10, descending=True, after=None)
    assert {c.id for c in page} >= {a.id, b.id}

    newest = page[0]
    after = await repo.list_components(
        app_session, limit=10, descending=True, after=(newest.created_at, newest.id)
    )
    assert newest.id not in {c.id for c in after}

    assert await repo.archive(app_session, a.id) is True
    assert await repo.get(app_session, a.id) is None  # archived -> excluded
    assert await repo.archive(app_session, a.id) is False  # already gone


@pytest.mark.asyncio
async def test_cross_org_isolation(two_orgs, app_session) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    a = await repo.create(app_session, config={"name": "A", "nodes": []})
    component_id = a.id
    await set_tenant_context(app_session, org_b)
    assert await repo.get(app_session, component_id) is None
    all_rows = await repo.list_components(app_session, limit=100, descending=True, after=None)
    assert component_id not in {c.id for c in all_rows}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd apps/api && uv run pytest tests/test_conversation_flow_components_repo.py -v
```
Expected: FAIL — `AttributeError` (`ids.encode_conversation_flow_component_id` missing) / `ModuleNotFoundError` (repo module missing).

- [ ] **Step 3: Add the id codec**

In `apps/api/src/usan_api/compat/ids.py`, add the prefix constant next to the others (after `_CONVERSATION_FLOW_PREFIX`, ~line 26):

```python
_CONVERSATION_FLOW_COMPONENT_PREFIX = "conversation_flow_component_"
```

Add these functions after `decode_conversation_flow_id` (~line 94):

```python
def encode_conversation_flow_component_id(component_id: uuid.UUID) -> str:
    return _CONVERSATION_FLOW_COMPONENT_PREFIX + component_id.hex


def decode_conversation_flow_component_id(token: str) -> uuid.UUID:
    return _decode_hex(
        token,
        prefix=_CONVERSATION_FLOW_COMPONENT_PREFIX,
        kind="conversation_flow_component_id",
    )
```

Add the cursor pair after `decode_conversation_flow_cursor` (~line 124):

```python
def encode_conversation_flow_component_cursor(created_at: datetime, cid: uuid.UUID) -> str:
    """Opaque (created_at, id) keyset cursor (delegates to the shared helper)."""
    return _encode_keyset_cursor(created_at, cid)


def decode_conversation_flow_component_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor token back to (created_at, id). Raises CompatError(422) on any bad input."""
    return _decode_keyset_cursor(token)
```

> Note the flow-prefix collision test in Step 1: `decode_conversation_flow_component_id` must reject a bare `conversation_flow_<hex>` token. It does — `_decode_hex` requires the full `conversation_flow_component_` prefix, and a flow id lacks the `component_` segment, so `str.startswith` fails → `CompatError(422)`.

- [ ] **Step 4: Write the repository**

Create `apps/api/src/usan_api/repositories/conversation_flow_components.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationFlowComponent


async def create(db: AsyncSession, *, config: dict[str, Any]) -> ConversationFlowComponent:
    component = ConversationFlowComponent(config=config)
    db.add(component)
    await db.flush()
    await db.refresh(component)
    return component


async def get(db: AsyncSession, component_id: uuid.UUID) -> ConversationFlowComponent | None:
    result = await db.execute(
        select(ConversationFlowComponent).where(
            ConversationFlowComponent.id == component_id,
            ConversationFlowComponent.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def update(
    db: AsyncSession, component_id: uuid.UUID, *, config: dict[str, Any]
) -> ConversationFlowComponent | None:
    component = await get(db, component_id)
    if component is None:
        return None
    component.config = config
    await db.flush()
    await db.refresh(component)
    return component


async def archive(db: AsyncSession, component_id: uuid.UUID) -> bool:
    component = await get(db, component_id)
    if component is None:
        return False
    component.archived_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_components(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[ConversationFlowComponent]:
    """Keyset-paginate the org's non-archived components over (created_at, id). RLS scopes to the
    caller's org. Fetches limit+1 so the caller computes has_more without a COUNT."""
    stmt = select(ConversationFlowComponent).where(
        ConversationFlowComponent.archived_at.is_(None)
    )
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    ConversationFlowComponent.created_at < after_created_at,
                    and_(
                        ConversationFlowComponent.created_at == after_created_at,
                        ConversationFlowComponent.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    ConversationFlowComponent.created_at > after_created_at,
                    and_(
                        ConversationFlowComponent.created_at == after_created_at,
                        ConversationFlowComponent.id > after_id,
                    ),
                )
            )
    if descending:
        stmt = stmt.order_by(
            ConversationFlowComponent.created_at.desc(), ConversationFlowComponent.id.desc()
        )
    else:
        stmt = stmt.order_by(
            ConversationFlowComponent.created_at.asc(), ConversationFlowComponent.id.asc()
        )
    stmt = stmt.limit(limit + 1)
    return list((await db.execute(stmt)).scalars().all())
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd apps/api && uv run pytest tests/test_conversation_flow_components_repo.py -v
```
Expected: PASS (all cases).

- [ ] **Step 6: Lint + typecheck**

Run:
```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/ids.py \
        apps/api/src/usan_api/repositories/conversation_flow_components.py \
        apps/api/tests/test_conversation_flow_components_repo.py
git commit -m "feat(api): conversation-flow-component id codec + repository (Phase 6b)"
```

---

### Task 3: Request schemas + serializer

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/conversation_flow_component.py`
- Test: `apps/api/tests/test_conversation_flow_component_schemas.py`

**Interfaces:**
- Consumes: `ids.encode_conversation_flow_component_id` (Task 2); `ConversationFlowComponent` (Task 1).
- Produces:
  - `CreateConversationFlowComponentRequest` (fields `name: str`, `nodes: list[Any]`; `extra="allow"`)
  - `UpdateConversationFlowComponentRequest` (`extra="allow"`, no declared fields)
  - `serialize_component(row: ConversationFlowComponent) -> dict[str, Any]`

- [ ] **Step 1: Write the schema tests (failing)**

Create `apps/api/tests/test_conversation_flow_component_schemas.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.conversation_flow_component import (
    CreateConversationFlowComponentRequest,
    UpdateConversationFlowComponentRequest,
    serialize_component,
)
from usan_api.db.models import ConversationFlowComponent


def test_create_request_requires_name_and_nodes() -> None:
    with pytest.raises(ValidationError):
        CreateConversationFlowComponentRequest(nodes=[])  # missing name
    with pytest.raises(ValidationError):
        CreateConversationFlowComponentRequest(name="Collector")  # missing nodes


def test_create_request_captures_extras() -> None:
    body = CreateConversationFlowComponentRequest(
        name="Collector",
        nodes=[{"id": "n1", "type": "conversation"}],
        flex_mode=True,
        tools=[{"type": "end_call"}],
    )
    dumped = body.model_dump()
    assert dumped["name"] == "Collector"
    assert dumped["flex_mode"] is True
    assert dumped["tools"] == [{"type": "end_call"}]
    assert dumped["nodes"] == [{"id": "n1", "type": "conversation"}]


def test_update_request_is_opaque_partial() -> None:
    body = UpdateConversationFlowComponentRequest(flex_mode=False)
    assert body.model_dump() == {"flex_mode": False}


def test_serialize_component_echoes_config_and_server_fields() -> None:
    cid = uuid.uuid4()
    row = ConversationFlowComponent(config={"name": "Collector", "flex_mode": True})
    row.id = cid
    row.updated_at = datetime(2026, 6, 30, tzinfo=UTC)
    out = serialize_component(row)
    assert out["conversation_flow_component_id"] == "conversation_flow_component_" + cid.hex
    assert out["name"] == "Collector"
    assert out["flex_mode"] is True
    assert "version" not in out  # components have no version field
    expected_ms = int(datetime(2026, 6, 30, tzinfo=UTC).timestamp() * 1000)
    assert out["user_modified_timestamp"] == expected_ms
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd apps/api && uv run pytest tests/test_conversation_flow_component_schemas.py -v
```
Expected: FAIL — `ModuleNotFoundError: ...schemas.conversation_flow_component`.

- [ ] **Step 3: Write the schemas + serializer**

Create `apps/api/src/usan_api/compat/schemas/conversation_flow_component.py`:

```python
"""RetellAI-compat conversation-flow-component request/response schemas + serializer (Phase 6b).

The component body is captured opaquely (extra='allow'): only the 2 oracle-required create fields
(name, nodes) are presence-checked; every other field rides through unvalidated and is
persisted/echoed verbatim (persisted-not-honored — semantic validation is the runtime's job).
serialize_component echoes the stored body + the 2 server-generated fields. No version field:
the oracle ConversationFlowComponentResponse has none.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat import ids
from usan_api.db.models import ConversationFlowComponent


class CreateConversationFlowComponentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    nodes: list[Any]


class UpdateConversationFlowComponentRequest(BaseModel):
    """Oracle ConversationFlowComponent: every field optional. Opaque — any subset of top-level
    fields is accepted and shallow-merged over the stored config by the router."""

    model_config = ConfigDict(extra="allow")


def serialize_component(row: ConversationFlowComponent) -> dict[str, Any]:
    data: dict[str, Any] = dict(row.config)
    data["conversation_flow_component_id"] = ids.encode_conversation_flow_component_id(row.id)
    data["user_modified_timestamp"] = int(row.updated_at.timestamp() * 1000)
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd apps/api && uv run pytest tests/test_conversation_flow_component_schemas.py -v
```
Expected: PASS.

- [ ] **Step 5: Lint + typecheck**

Run:
```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/conversation_flow_component.py \
        apps/api/tests/test_conversation_flow_component_schemas.py
git commit -m "feat(api): conversation-flow-component request schemas + serializer (Phase 6b)"
```

---

### Task 4: Router + mount + promote stubs + CRUD tests + fidelity update

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/conversation_flow_component.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (import + include_router)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove 5 stub tuples)
- Modify: `apps/api/tests/test_compat_fidelity.py` (retarget the two `/create-conversation-flow-component` references)
- Test: `apps/api/tests/compat/test_conversation_flow_component_crud.py`

**Interfaces:**
- Consumes: repo `conversation_flow_components` (Task 2), schemas + `serialize_component` (Task 3), `ids` codec (Task 2), `get_compat_db` / `CompatError` (existing compat plumbing).
- Produces: 5 served routes at exact versioned paths; `conversation_flow_component.router`.

- [ ] **Step 1: Write the CRUD test (failing)**

Create `apps/api/tests/compat/test_conversation_flow_component_crud.py`:

```python
from __future__ import annotations

import uuid

_COMPONENT = {"name": "Collector", "nodes": []}


def _create(compat_client, compat_headers, **extra) -> dict:
    r = compat_client.post(
        "/create-conversation-flow-component",
        json={**_COMPONENT, **extra},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers, flex_mode=True)
    cid = body["conversation_flow_component_id"]
    assert cid.startswith("conversation_flow_component_")
    assert isinstance(body["user_modified_timestamp"], int)
    assert body["name"] == "Collector"
    assert body["flex_mode"] is True
    assert "version" not in body
    g = compat_client.get(
        f"/get-conversation-flow-component/{cid}", headers=compat_headers
    )
    assert g.status_code == 200
    assert g.json()["conversation_flow_component_id"] == cid


def test_update_merges_top_level(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers, flex_mode=True)["conversation_flow_component_id"]
    u1 = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": False},
        headers=compat_headers,
    )
    assert u1.status_code == 200, u1.text
    assert u1.json()["flex_mode"] is False
    # Omitting flex_mode preserves it; a new top-level field is added.
    u2 = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"tools": [{"type": "end_call"}]},
        headers=compat_headers,
    )
    assert u2.status_code == 200
    body = u2.json()
    assert body["flex_mode"] is False  # preserved
    assert body["tools"] == [{"type": "end_call"}]


def test_update_null_clears_field(compat_client, compat_headers) -> None:
    cid = _create(
        compat_client, compat_headers, flex_mode=True
    )["conversation_flow_component_id"]
    u = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": None},
        headers=compat_headers,
    )
    assert u.status_code == 200
    assert "flex_mode" not in u.json()  # explicit null cleared it -> omitted from echo
    g = compat_client.get(
        f"/get-conversation-flow-component/{cid}", headers=compat_headers
    ).json()
    assert "flex_mode" not in g


def test_delete_then_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_component_id"]
    d = compat_client.delete(
        f"/delete-conversation-flow-component/{cid}", headers=compat_headers
    )
    assert d.status_code == 204
    assert d.content == b""
    r404 = compat_client.get(
        f"/get-conversation-flow-component/{cid}", headers=compat_headers
    )
    assert r404.status_code == 404


def test_update_after_delete_is_404(compat_client, compat_headers) -> None:
    cid = _create(compat_client, compat_headers)["conversation_flow_component_id"]
    d = compat_client.delete(
        f"/delete-conversation-flow-component/{cid}", headers=compat_headers
    )
    assert d.status_code == 204
    r = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": False},
        headers=compat_headers,
    )
    assert r.status_code == 404
    assert r.json() == {"status": 404, "message": "conversation flow component not found"}


def test_missing_required_field_is_422(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json={"name": "x"}, headers=compat_headers
    )
    assert r.status_code == 422


def test_malformed_id_is_422_and_missing_is_404(compat_client, compat_headers) -> None:
    r422 = compat_client.get(
        "/get-conversation-flow-component/not_a_component", headers=compat_headers
    )
    assert r422.status_code == 422
    missing = "conversation_flow_component_" + uuid.uuid4().hex
    r404 = compat_client.get(
        f"/get-conversation-flow-component/{missing}", headers=compat_headers
    )
    assert r404.status_code == 404


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    created = {
        _create(compat_client, compat_headers)["conversation_flow_component_id"]
        for _ in range(3)
    }
    r = compat_client.get(
        "/v2/list-conversation-flow-components?limit=2", headers=compat_headers
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    seen = {i["conversation_flow_component_id"] for i in body["items"]}
    key = body["pagination_key"]
    for _ in range(20):
        nxt = compat_client.get(
            f"/v2/list-conversation-flow-components?limit=2&pagination_key={key}",
            headers=compat_headers,
        ).json()
        seen |= {i["conversation_flow_component_id"] for i in nxt["items"]}
        if not nxt.get("has_more"):
            break
        key = nxt["pagination_key"]
    assert created <= seen


def test_server_keys_stripped_and_not_spoofable(
    compat_client, compat_headers, async_database_url
) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.compat import ids

    body = _create(
        compat_client,
        compat_headers,
        conversation_flow_component_id="conversation_flow_component_deadbeef",
        user_modified_timestamp=1,
    )
    # Server wins: the id is server-generated, not the client's spoof.
    assert body["conversation_flow_component_id"] != "conversation_flow_component_deadbeef"
    assert body["user_modified_timestamp"] != 1
    cid = ids.decode_conversation_flow_component_id(body["conversation_flow_component_id"])

    async def _read_config() -> dict:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(
                    text("SELECT config FROM conversation_flow_components WHERE id = :i"),
                    {"i": cid},
                )
        finally:
            await engine.dispose()

    stored = asyncio.run(_read_config())
    assert "conversation_flow_component_id" not in stored
    assert "user_modified_timestamp" not in stored
    assert stored["name"] == "Collector"  # real fields survive
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd apps/api && uv run pytest tests/compat/test_conversation_flow_component_crud.py -v
```
Expected: FAIL — routes return 501 (stub) / the module isn't mounted yet, so `create` returns 501 not 201.

- [ ] **Step 3: Write the router**

Create `apps/api/src/usan_api/compat/routers/conversation_flow_component.py`:

```python
"""RetellAI-compat conversation-flow-component CRUD (Phase 6b): create/get/update/delete/list.

The component body is persisted (JSONB) and echoed conformantly but NOT executed at call/chat
time (persisted-not-honored — the DAG runtime is a later sub-phase). A component is a standalone
entity (its own conversation_flow_components table). The session does not autocommit; each
mutation commits explicitly. Delete is a plain soft-delete: the oracle's "local copies for
linked flows" fan-out is not backed (nothing links components to flows at rest). See
docs/deployment/conversation-flow-components.md.
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
from usan_api.compat.schemas.conversation_flow_component import (
    CreateConversationFlowComponentRequest,
    UpdateConversationFlowComponentRequest,
    serialize_component,
)
from usan_api.repositories import conversation_flow_components as components_repo

router = APIRouter(tags=["compat-conversation-flow-components"])

# Server-generated response fields — never stored inside the component config. A client can inject
# them via extra='allow', but serialize_component always derives them from the ORM columns, so we
# drop them before persisting (defense-in-depth against a future reader of row.config[...]).
_SERVER_KEYS = ("conversation_flow_component_id", "user_modified_timestamp")


def _strip_server_keys(config: dict[str, Any]) -> dict[str, Any]:
    for _k in _SERVER_KEYS:
        config.pop(_k, None)
    return config


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the component config (it can carry prompts).
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat conversation-flow-component op={op}")


def _provided(
    model: CreateConversationFlowComponentRequest | UpdateConversationFlowComponentRequest,
) -> dict[str, Any]:
    # Drop null-valued keys (declared and extra='allow') so we store/merge only real values.
    return {k: v for k, v in model.model_dump().items() if v is not None}


@router.post("/create-conversation-flow-component", status_code=status.HTTP_201_CREATED)
async def create_conversation_flow_component(
    body: CreateConversationFlowComponentRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    config = _strip_server_keys(_provided(body))
    row = await components_repo.create(db, config=config)
    await db.commit()
    _audit(request, "create-conversation-flow-component")
    return serialize_component(row)


@router.get("/get-conversation-flow-component/{conversation_flow_component_id}")
async def get_conversation_flow_component(
    conversation_flow_component_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    row = await components_repo.get(db, component_id)
    if row is None:
        raise CompatError(404, "conversation flow component not found")
    _audit(request, "get-conversation-flow-component")
    return serialize_component(row)


@router.patch("/update-conversation-flow-component/{conversation_flow_component_id}")
async def update_conversation_flow_component(
    conversation_flow_component_id: str,
    body: UpdateConversationFlowComponentRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    row = await components_repo.get(db, component_id)
    if row is None:
        raise CompatError(404, "conversation flow component not found")
    # Top-level shallow merge: a sent non-null field overwrites; a sent explicit null CLEARS the
    # field (removed -> omitted from the echo, matching the oracle's omit-nulls responses); an
    # omitted field is preserved. body.model_dump() carries the extra='allow' fields incl nulls.
    merged = dict(row.config)
    for _key, _value in body.model_dump().items():
        if _value is None:
            merged.pop(_key, None)
        else:
            merged[_key] = _value
    _strip_server_keys(merged)
    updated = await components_repo.update(db, component_id, config=merged)
    if updated is None:
        raise CompatError(404, "conversation flow component not found")
    await db.commit()
    _audit(request, "update-conversation-flow-component")
    return serialize_component(updated)


@router.delete(
    "/delete-conversation-flow-component/{conversation_flow_component_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation_flow_component(
    conversation_flow_component_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    component_id = ids.decode_conversation_flow_component_id(conversation_flow_component_id)
    # Plain soft-delete. The oracle's "creates local copies for all linked conversation flows"
    # fan-out is not backed (nothing links components to flows at rest) — documented, not faked.
    if not await components_repo.archive(db, component_id):
        raise CompatError(404, "conversation flow component not found")
    await db.commit()
    _audit(request, "delete-conversation-flow-component")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v2/list-conversation-flow-components")
async def list_conversation_flow_components(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after = None
    if pagination_key:
        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after = ids.decode_conversation_flow_component_cursor(pagination_key)
    rows = await components_repo.list_components(
        db, limit=limit, descending=(sort_order != "ascending"), after=after
    )
    _audit(request, "list-conversation-flow-components")
    has_more = len(rows) > limit
    page = rows[:limit]
    out: dict[str, Any] = {
        "items": [serialize_component(r) for r in page],
        "has_more": has_more,
    }
    if has_more:
        out["pagination_key"] = ids.encode_conversation_flow_component_cursor(
            page[-1].created_at, page[-1].id
        )
    return out
```

- [ ] **Step 4: Mount the router**

In `apps/api/src/usan_api/compat/app.py`, add the import after the `conversation_flow` import (line 24):

```python
from usan_api.compat.routers import conversation_flow_component as compat_conversation_flow_component
```

Add the include after `app.include_router(compat_conversation_flow.router)` (line 68):

```python
    app.include_router(compat_conversation_flow_component.router)
```

- [ ] **Step 5: Remove the 5 stub tuples**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete these six lines (the `# --- Conversation flow component ---` comment and its 5 tuples, lines 26–31):

```python
    # --- Conversation flow component ---
    ("POST", "/create-conversation-flow-component"),
    ("GET", "/v2/list-conversation-flow-components"),
    ("GET", "/get-conversation-flow-component/{conversation_flow_component_id}"),
    ("PATCH", "/update-conversation-flow-component/{conversation_flow_component_id}"),
    ("DELETE", "/delete-conversation-flow-component/{conversation_flow_component_id}"),
```

- [ ] **Step 6: Retarget the fidelity test references**

In `apps/api/tests/test_compat_fidelity.py`, the parametrize list (lines 113–122) currently lists `("post", "/create-conversation-flow-component")` first — remove that one line (the remaining four stubs still exercise the 501 path). The list becomes:

```python
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/clone-voice"),
        ("get", "/get-mcp-tools/some-agent-id"),
        ("post", "/agent-playground-completion/some-agent-id"),
        ("post", "/create-phone-number"),
    ],
)
```

And in `test_unsupported_still_requires_key` (line 142), swap the now-served path for a still-unsupported one:

```python
def test_unsupported_still_requires_key(compat_client):
    # The app-level auth gate runs before the stub: no key → 401, not 501.
    assert compat_client.post("/clone-voice", json={}).status_code == 401
```

- [ ] **Step 7: Run the CRUD test + fidelity test to verify they pass**

Run:
```bash
cd apps/api && uv run pytest tests/compat/test_conversation_flow_component_crud.py tests/test_compat_fidelity.py -v
```
Expected: PASS (all CRUD cases + fidelity 501/auth cases green).

- [ ] **Step 8: Lint + typecheck**

Run:
```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
```
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add apps/api/src/usan_api/compat/routers/conversation_flow_component.py \
        apps/api/src/usan_api/compat/app.py \
        apps/api/src/usan_api/compat/routers/unsupported.py \
        apps/api/tests/compat/test_conversation_flow_component_crud.py \
        apps/api/tests/test_compat_fidelity.py
git commit -m "feat(api): conversation-flow-component CRUD router + mount; promote 5 stubs to served (Phase 6b)"
```

---

### Task 5: Conformance freeze

**Files:**
- Test: `apps/api/tests/compat/test_freeze_conversation_flow_components.py`

**Interfaces:**
- Consumes: served routes (Task 4); `assert_conforms` / `assert_sdk_roundtrip` (existing `tests/compat/conformance.py`); oracle schema `ConversationFlowComponentResponse`; SDK types `retell.types:ConversationFlowComponentResponse` and `retell.types:ConversationFlowComponentListResponse` (both verified present).

- [ ] **Step 1: Write the freeze test**

Create `apps/api/tests/compat/test_freeze_conversation_flow_components.py`:

```python
"""Frozen conformance for the compat conversation-flow-component surface (Phase 6b)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen

_COMPONENT = {
    "name": "Customer Information Collector",
    "nodes": [],
    "flex_mode": False,
}


def test_create_conforms(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json=_COMPONENT, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["conversation_flow_component_id"].startswith("conversation_flow_component_")
    assert isinstance(body["user_modified_timestamp"], int)
    assert_conforms(body, "ConversationFlowComponentResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowComponentResponse")


def test_get_update_list_conform(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/create-conversation-flow-component", json=_COMPONENT, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    cid = r.json()["conversation_flow_component_id"]

    g = compat_client.get(
        f"/get-conversation-flow-component/{cid}", headers=compat_headers
    )
    assert g.status_code == 200
    assert_conforms(g.json(), "ConversationFlowComponentResponse")

    u = compat_client.patch(
        f"/update-conversation-flow-component/{cid}",
        json={"flex_mode": True},
        headers=compat_headers,
    )
    assert u.status_code == 200
    assert_conforms(u.json(), "ConversationFlowComponentResponse")

    lst = compat_client.get(
        "/v2/list-conversation-flow-components?limit=2", headers=compat_headers
    )
    assert lst.status_code == 200
    body = lst.json()
    assert isinstance(body["items"], list)
    for item in body["items"]:
        assert_conforms(item, "ConversationFlowComponentResponse")
    assert_sdk_roundtrip(body, "retell.types:ConversationFlowComponentListResponse")
```

- [ ] **Step 2: Run the freeze test to verify it passes**

Run:
```bash
cd apps/api && uv run pytest tests/compat/test_freeze_conversation_flow_components.py -v
```
Expected: PASS. If `assert_sdk_roundtrip` for the list envelope fails because the SDK `ConversationFlowComponentListResponse` is a bare-list root model, drop that single line (the per-item `assert_conforms` loop above already freezes the item shape) — mirror whatever 6a's list-roundtrip did if it diverges.

- [ ] **Step 3: Run the full compat suite (no regressions)**

Run:
```bash
cd apps/api && uv run pytest tests/compat -q
```
Expected: PASS (no other compat surface regressed).

- [ ] **Step 4: Lint + typecheck**

Run:
```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/compat/test_freeze_conversation_flow_components.py
git commit -m "test(api): freeze conversation-flow-component conformance (oracle + SDK) (Phase 6b)"
```

---

### Task 6: Operator note + full-gate verification

**Files:**
- Create: `docs/deployment/conversation-flow-components.md`

- [ ] **Step 1: Write the operator note**

Create `docs/deployment/conversation-flow-components.md`:

```markdown
# Conversation-Flow Components (RetellAI-compat, Phase 6b)

Five endpoints — `create` / `get` / `update` (PATCH) / `delete` / `v2/list` — for **shared
conversation-flow components**, on their own `conversation_flow_components` table (migration 0049,
FORCE row-level-security, per-org isolated).

## Status: persisted-not-honored

The component body (`name`, `nodes`, `flex_mode`, `tools`, `mcps`, …) is stored as opaque JSONB and
echoed back conformantly. It is **not executed** at call/chat time — the DAG runtime is a later
sub-phase. Only the 2 oracle-required create fields (`name`, `nodes`) are validated; everything
else rides through verbatim.

## Delete semantics

The RetellAI oracle documents delete as "creates local copies for all linked conversation flows."
We **do not back** that fan-out — nothing links components to flows at rest (components ride inside
flow `config` as opaque JSON, and there is no runtime linking layer yet). `DELETE` performs a plain
soft-delete (sets `archived_at`) and returns 204. When the DAG runtime lands, revisit this.

## Server-owned fields

`conversation_flow_component_id` and `user_modified_timestamp` are always derived from the row and
stripped from stored config — a client cannot spoof them. Note: unlike conversation flows,
components have **no `version`** field (the oracle response omits it).

## Deploy

Merged ≠ deployed. This surface is inert until the next `v*` tag deploy runs migration 0049 and
ships the new router. It requires no new env keys.
```

- [ ] **Step 2: Run the full API gate**

Run:
```bash
cd apps/api && uv run pytest -q && ruff check . && ruff format --check . && uv run mypy
```
Expected: entire suite PASS (2275+ prior tests + the new ~26), ruff clean, mypy clean.

- [ ] **Step 3: Confirm single Alembic head**

Run:
```bash
cd apps/api && uv run alembic heads
```
Expected: single head `0049 (head)`.

- [ ] **Step 4: Commit**

```bash
git add docs/deployment/conversation-flow-components.md
git commit -m "docs(api): conversation-flow-component CRUD operator note (Phase 6b)"
```

---

## Self-Review notes

- **Spec coverage:** §3 migration → Task 1; §4 codec → Task 2; §5 schemas → Task 3; §6 repo → Task 2; §7 router + stub-promotion → Task 4; §8 tests → Tasks 1–5; §9 operator note → Task 6. All covered.
- **The `no version` divergence** is asserted explicitly in Task 1 (no column), Task 3 (`"version" not in out`), and Task 4 (`"version" not in body`).
- **Null-clear + server-key-strip + TOCTOU-404** are the three behaviors the 6a `/review` hardened; they are ported verbatim and each has a dedicated test.
- **Type consistency:** repo funcs `create(config=)`, `update(config=)`, `archive`, `list_components`, `get`; serializer `serialize_component`; codec `encode/decode_conversation_flow_component_id` + `_cursor` — names match across Tasks 2–5.
- **Post-merge:** cut nothing; this rides the next `v*` tag with the rest of the un-deployed 4b–6a backlog.
