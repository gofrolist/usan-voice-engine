# RetellAI Parity Phase 4a — Chat (api_chat) Sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve RetellAI's 7 `api_chat` chat-session operations from the compat sub-app, backed by a live Vertex AI text turn, with dedicated `chat_sessions`/`chat_messages` tables.

**Architecture:** A new `compat/routers/chats.py` mounts 7 routes on the existing compat FastAPI sub-app. Sessions/messages persist in two new `TenantScoped` + FORCE-RLS tables (migration `0042`). `create-chat-completion` reuses the existing in-`apps/api` Vertex path (`vertex_test.run_vertex_turn` + `prompt_substitution.substitute`/`build_vars`) — text-only, `tools=[]` — with no LiveKit and no `services/agent` import. Serialization mirrors `serialize_call`'s `include_transcript` pattern; conformance is asserted against the vendored oracle (`ChatResponse`/`V3ChatResponse`/`Message`) and the `retell-sdk` concrete types.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, Postgres native enums + RLS, `google-genai` (Vertex), `openapi-schema-validator`, `retell-sdk==5.53.0`, pytest (testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-06-26-retell-parity-phase4a-chat-sessions-design.md`

## Global Constraints

- **Compat envelope:** every error is `CompatError(status, message)` → `{"status": int, "message": str}` (handlers already registered on the sub-app). Not-found → 404; bad id / unpublished agent / reserved-prefix key / completion-to-ended → 422; Vertex misconfig → 503; Vertex failure → 502.
- **PHI/secret-safe:** never log `str(exc)`, request bodies, reply text, dynamic vars, or metadata. Caught exceptions → `raise CompatError(...) from None`; the global handler logs `type(exc).__name__` only. Audit logs carry org id + op + chat id only.
- **`response_model_exclude_none=True`** on every route. All timestamps are Unix epoch **milliseconds** (`to_ms`).
- **RLS:** both new tables `TenantScoped` + FORCE RLS; `organization_id` is DB-`server_default`-filled from `app.current_org` — **never** set by app code.
- **Vertex:** `vertexai=True` + ADC only; never the Gemini Developer API. Text-only (`tools=[]`).
- **Compat session is NOT autocommit** — every mutation ends with an explicit `await db.commit()`.
- **`apps/api` and `services/agent` must NOT import each other.**
- **Commit format:** `type(scope): description`, scope `api`. No attribution footer.
- **Surface coverage:** when a route is served, remove its `(METHOD, path)` from `_UNSUPPORTED` and from the `test_compat_fidelity.py` parametrize in the SAME change; run the FULL `apps/api` suite. `KNOWN_GAPS` stays `frozenset()`.
- **CI:** `uv run mypy` (files=src) + `ruff check .` + `ruff format .` must pass. Run `cd apps/api && uv run pytest` (parallel) and `uv run pytest -n0 <file>` for single-file debugging.
- **Migration is owner-DDL** (next revision `0042`, `down_revision="0041"`); inert until an operator deploys it + sets `GCP_PROJECT`.

## File Structure

| File | Responsibility |
|---|---|
| `apps/api/src/usan_api/db/base.py` | + `ChatStatus` enum (Task 1) |
| `apps/api/src/usan_api/db/models.py` | + `ChatSession`, `ChatMessage` models (Task 1) |
| `apps/api/migrations/versions/0042_chat_sessions.py` | create the two tables + `chat_status` enum + RLS (Task 1) |
| `apps/api/src/usan_api/compat/ids.py` | + `encode_chat_id`/`decode_chat_id`/`encode_message_id` (Task 2) |
| `apps/api/src/usan_api/compat/schemas/chats.py` | request + response Pydantic models (Task 3) |
| `apps/api/src/usan_api/compat/chat_serializer.py` | `serialize_chat` (Task 4) |
| `apps/api/src/usan_api/repositories/chats.py` | session/message persistence (Task 4) |
| `apps/api/src/usan_api/compat/chat_service.py` | the 7 service functions incl. the live Vertex turn (Task 5) |
| `apps/api/src/usan_api/compat/routers/chats.py` | the 7 routes (Task 6) |
| `apps/api/src/usan_api/compat/app.py` | register the chats router (Task 6) |
| `apps/api/src/usan_api/compat/routers/unsupported.py` | remove the 7 api_chat entries (Task 6) |
| `apps/api/tests/test_compat_fidelity.py` | drop `("post","/create-chat")` from the 501 parametrize (Task 6) |
| `apps/api/tests/compat/test_freeze_chats.py` | conformance + behavioral freeze suite (Task 6) |
| `apps/api/tests/compat/conformance.py` | + chat discovery-map comment (Task 7) |
| `docs/deployment/chat-sessions-vertex.md` | operator note (Task 7) |

---

### Task 1: ChatStatus enum + chat_sessions/chat_messages models + migration 0042

**Files:**
- Modify: `apps/api/src/usan_api/db/base.py`
- Modify: `apps/api/src/usan_api/db/models.py`
- Create: `apps/api/migrations/versions/0042_chat_sessions.py`
- Test: `apps/api/tests/compat/test_chat_models.py`

**Interfaces:**
- Produces: `ChatStatus(ONGOING="ongoing", ENDED="ended", ERROR="error")`; `ChatSession` (`id, organization_id, agent_profile_id, agent_version:int, status:ChatStatus, chat_type:str, dynamic_vars:dict, custom_attributes:dict, started_at, ended_at, archived_at, created_at, updated_at`); `ChatMessage` (`id, organization_id, chat_session_id, seq:int, role:str, content:str, created_at`); native enum `chat_status`; `UNIQUE(chat_session_id, seq)`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_models.py
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession


@pytest.mark.asyncio
async def test_chat_session_and_message_persist_under_rls(app_session, set_tenant_context):
    """A ChatSession + ChatMessage insert under an org context round-trips, org_id auto-filled."""
    async with app_session() as db:
        await set_tenant_context(db)
        profile_id = (
            await db.execute(text("SELECT id FROM agent_profiles LIMIT 1"))
        ).scalar_one()
        chat = ChatSession(
            agent_profile_id=profile_id,
            agent_version=1,
            status=ChatStatus.ONGOING,
            chat_type="api_chat",
            dynamic_vars={"name": "Pat"},
        )
        db.add(chat)
        await db.flush()
        assert chat.organization_id is not None  # DB default filled it
        db.add(ChatMessage(chat_session_id=chat.id, seq=1, role="user", content="hi"))
        await db.flush()
        loaded = (
            await db.execute(select(ChatSession).where(ChatSession.id == chat.id))
        ).scalar_one()
        assert loaded.status is ChatStatus.ONGOING
        assert loaded.chat_type == "api_chat"
        await db.rollback()


@pytest.mark.asyncio
async def test_chat_message_seq_is_unique_per_session(app_session, set_tenant_context):
    """A duplicate (chat_session_id, seq) violates the unique constraint."""
    from sqlalchemy.exc import IntegrityError

    async with app_session() as db:
        await set_tenant_context(db)
        profile_id = (
            await db.execute(text("SELECT id FROM agent_profiles LIMIT 1"))
        ).scalar_one()
        chat = ChatSession(agent_profile_id=profile_id, agent_version=1, dynamic_vars={})
        db.add(chat)
        await db.flush()
        db.add(ChatMessage(chat_session_id=chat.id, seq=1, role="user", content="a"))
        await db.flush()
        db.add(ChatMessage(chat_session_id=chat.id, seq=1, role="agent", content="b"))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
```

> If the `app_session` / `set_tenant_context` fixtures differ in name in `tests/conftest.py`, adapt to the real org-scoped session fixture (the implementer confirms the actual fixture that yields an RLS session bound to the seeded `usan` org). The seeded `agent_profiles` row exists from the connect-baseline seed.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'ChatStatus'` / `ChatSession`.

- [ ] **Step 3: Add the `ChatStatus` enum to `db/base.py`**

Append after the `CallType` class (mirrors its exact form):

```python
class ChatStatus(enum.Enum):
    ONGOING = "ongoing"
    ENDED = "ended"
    ERROR = "error"
```

- [ ] **Step 4: Add the models to `db/models.py`**

Import `ChatStatus` from `usan_api.db.base` (add to the existing base-enum import line). Add the two models (mirroring the `Call` idioms — `SAEnum(..., values_callable=_enum_values, create_type=False)`, `TenantScoped`, JSONB `server_default text("'{}'")`, `func.now()`):

```python
class ChatSession(Base, TenantScoped):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_started_at_id", "started_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id"), nullable=False
    )
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ChatStatus] = mapped_column(
        SAEnum(ChatStatus, name="chat_status", values_callable=_enum_values, create_type=False),
        nullable=False,
        server_default=ChatStatus.ONGOING.value,
    )
    chat_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'api_chat'"))
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    custom_attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChatMessage(Base, TenantScoped):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("chat_session_id", "seq", name="uq_chat_messages_session_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

> Confirm `Index` and `UniqueConstraint` are already imported in `models.py` (they are used by existing models); add to the import line if missing.

- [ ] **Step 5: Write migration `0042_chat_sessions.py`**

```python
"""chat_sessions + chat_messages: TenantScoped + FORCE RLS tables for the api_chat surface.

New owner-DDL tables (modeled on 0040). chat_status is a native enum (ongoing|ended|error).
GRANT to usan_app so the least-priv runtime role can CRUD them.

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
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
    chat_status = postgresql.ENUM("ongoing", "ended", "error", name="chat_status", create_type=False)
    chat_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("agent_profile_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("status", chat_status, server_default="ongoing", nullable=False),
        sa.Column("chat_type", sa.Text(), server_default=sa.text("'api_chat'"), nullable=False),
        sa.Column("dynamic_vars", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "custom_attributes", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["agent_profile_id"], ["agent_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_sessions_organization_id", "chat_sessions", ["organization_id"])
    op.create_index("ix_chat_sessions_started_at_id", "chat_sessions", ["started_at", "id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("chat_session_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_session_id", "seq", name="uq_chat_messages_session_seq"),
    )
    op.create_index("ix_chat_messages_organization_id", "chat_messages", ["organization_id"])
    op.create_index("ix_chat_messages_session_seq", "chat_messages", ["chat_session_id", "seq"])

    _enable_rls("chat_sessions")
    _enable_rls("chat_messages")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_messages")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_sessions")
    op.drop_index("ix_chat_messages_session_seq", table_name="chat_messages")
    op.drop_index("ix_chat_messages_organization_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_started_at_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_organization_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    postgresql.ENUM(name="chat_status", create_type=False).drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_models.py -v`
Expected: PASS (the testcontainer applies `alembic upgrade head` including 0042). Then `uv run alembic heads` shows a single head `0042`.

- [ ] **Step 7: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/db/base.py apps/api/src/usan_api/db/models.py apps/api/migrations/versions/0042_chat_sessions.py apps/api/tests/compat/test_chat_models.py
git commit -m "feat(api): chat_sessions/chat_messages tables + ChatStatus enum (migration 0042)"
```

---

### Task 2: chat id codec (`encode/decode_chat_id`, `encode_message_id`)

**Files:**
- Modify: `apps/api/src/usan_api/compat/ids.py`
- Test: `apps/api/tests/compat/test_chat_ids.py`

**Interfaces:**
- Produces: `encode_chat_id(uuid) -> str` (`"chat_" + hex`); `decode_chat_id(str) -> uuid` (raises `CompatError(422)`); `encode_message_id(uuid) -> str` (`"message_" + hex`).

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_ids.py
from __future__ import annotations

import uuid

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError


def test_chat_id_round_trips():
    cid = uuid.uuid4()
    token = ids.encode_chat_id(cid)
    assert token.startswith("chat_")
    assert ids.decode_chat_id(token) == cid


def test_message_id_encodes_with_prefix():
    mid = uuid.uuid4()
    assert ids.encode_message_id(mid) == "message_" + mid.hex


@pytest.mark.parametrize("bad", ["nope", "chat_xyz", "agent_" + "0" * 32, ""])
def test_decode_chat_id_rejects_malformed(bad):
    with pytest.raises(CompatError) as exc:
        ids.decode_chat_id(bad)
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_ids.py -v`
Expected: FAIL — `AttributeError: module 'usan_api.compat.ids' has no attribute 'encode_chat_id'`.

- [ ] **Step 3: Add the codec to `ids.py`**

Add a prefix constant next to the others and the three functions (mirroring `encode_agent_id`/`decode_agent_id`):

```python
_CHAT_PREFIX = "chat_"
_MESSAGE_PREFIX = "message_"


def encode_chat_id(chat_id: uuid.UUID) -> str:
    return _CHAT_PREFIX + chat_id.hex


def decode_chat_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_CHAT_PREFIX, kind="chat_id")


def encode_message_id(message_id: uuid.UUID) -> str:
    return _MESSAGE_PREFIX + message_id.hex
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_ids.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/compat/ids.py apps/api/tests/compat/test_chat_ids.py
git commit -m "feat(api): chat_id/message_id codec in compat ids"
```

---

### Task 3: compat chat schemas (`compat/schemas/chats.py`)

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/chats.py`
- Test: `apps/api/tests/compat/test_chat_schemas.py`

**Interfaces:**
- Produces: `CreateChatRequest` (`agent_id:str` min_length 1; `agent_version:int|str|None`; `metadata:dict|None`; `retell_llm_dynamic_variables:dict|None`); `CreateChatCompletionRequest` (`chat_id:str`, `content:str`); `UpdateChatRequest` (`metadata`, `data_storage_setting:Literal["everything","basic_attributes_only"]|None`, `override_dynamic_variables`, `custom_attributes`); `ListChatsRequest` (mirror `ListCallsRequest`); `CompatChatMessage`; `CompatChat`; `CompatChatCompletion`; `ListChatsResponse`. All request models `extra="forbid"`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_schemas.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.chats import (
    CompatChat,
    CreateChatCompletionRequest,
    CreateChatRequest,
    ListChatsRequest,
    UpdateChatRequest,
)


def test_create_chat_requires_agent_id():
    with pytest.raises(ValidationError):
        CreateChatRequest()  # type: ignore[call-arg]
    m = CreateChatRequest(agent_id="agent_x", retell_llm_dynamic_variables={"n": "p"})
    assert m.agent_id == "agent_x"


def test_create_chat_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        CreateChatRequest(agent_id="agent_x", bogus=1)  # type: ignore[call-arg]


def test_completion_requires_chat_id_and_content():
    with pytest.raises(ValidationError):
        CreateChatCompletionRequest(chat_id="chat_x")  # type: ignore[call-arg]
    assert CreateChatCompletionRequest(chat_id="chat_x", content="hi").content == "hi"


def test_update_chat_data_storage_setting_enum():
    UpdateChatRequest(data_storage_setting="everything")
    with pytest.raises(ValidationError):
        UpdateChatRequest(data_storage_setting="nonsense")


def test_list_chats_skip_xor_pagination_key():
    ListChatsRequest(skip=5)
    ListChatsRequest(pagination_key="chat_x")
    with pytest.raises(ValidationError):
        ListChatsRequest(skip=5, pagination_key="chat_x")


def test_compat_chat_omits_empty_optionals():
    c = CompatChat(chat_id="chat_x", agent_id="agent_y", chat_status="ongoing")
    dumped = c.model_dump(exclude_none=True)
    assert dumped == {
        "chat_id": "chat_x",
        "agent_id": "agent_y",
        "chat_status": "ongoing",
        "chat_type": "api_chat",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.compat.schemas.chats'`.

- [ ] **Step 3: Write `compat/schemas/chats.py`**

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateChatRequest(BaseModel):
    """POST /create-chat. Oracle: agent_id required; the rest optional."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None


class CreateChatCompletionRequest(BaseModel):
    """POST /create-chat-completion. chat_id + content required; no vars/metadata."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    content: str


class UpdateChatRequest(BaseModel):
    """PATCH /update-chat. All optional. data_storage_setting is accepted-and-ignored (4a)."""

    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] | None = None
    data_storage_setting: Literal["everything", "basic_attributes_only"] | None = None
    override_dynamic_variables: dict[str, str] | None = None
    custom_attributes: dict[str, Any] | None = None


class ListChatsRequest(BaseModel):
    """POST /v3/list-chats — filterable, cursor- (or skip-) paginated. Mirrors ListCallsRequest."""

    model_config = ConfigDict(extra="forbid")

    filter_criteria: dict[str, Any] | None = None
    sort_order: str = "descending"
    limit: int = Field(default=50, ge=1, le=1000)
    pagination_key: str | None = None
    skip: int | None = Field(default=None, ge=0)
    include_total: bool = False

    @model_validator(mode="after")
    def _skip_xor_pagination_key(self) -> "ListChatsRequest":
        if self.skip is not None and self.pagination_key is not None:
            raise ValueError("skip and pagination_key are mutually exclusive")
        return self


class CompatChatMessage(BaseModel):
    role: str
    content: str
    message_id: str
    created_timestamp: int


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


class CompatChatCompletion(BaseModel):
    messages: list[CompatChatMessage] = Field(default_factory=list)


class ListChatsResponse(BaseModel):
    items: list[CompatChat] = Field(default_factory=list)
    pagination_key: str | None = None
    has_more: bool = False
    total: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_schemas.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/compat/schemas/chats.py apps/api/tests/compat/test_chat_schemas.py
git commit -m "feat(api): compat chat request/response schemas"
```

---

### Task 4: chat repository + serializer

**Files:**
- Create: `apps/api/src/usan_api/repositories/chats.py`
- Create: `apps/api/src/usan_api/compat/chat_serializer.py`
- Test: `apps/api/tests/compat/test_chat_repo_serializer.py`

**Interfaces:**
- Consumes: Task 1 models, Task 2 ids, Task 3 `CompatChat`/`CompatChatMessage`, `serialization.{to_ms, unpack_dynamic_vars}`.
- Produces (repository): `add_session(db, *, agent_profile_id, agent_version, dynamic_vars) -> ChatSession`; `get_session(db, session_id) -> ChatSession | None` (excludes archived); `lock_session(db, session_id) -> ChatSession | None` (`FOR UPDATE`, excludes archived); `next_seq(db, session_id) -> int`; `add_message(db, *, session_id, seq, role, content) -> ChatMessage`; `list_messages(db, session_id) -> list[ChatMessage]` (ordered by seq); `query_sessions(db, body) -> list[ChatSession]`; `count_sessions(db, body) -> int`.
- Produces (serializer): `serialize_chat(session, messages, *, include_transcript) -> CompatChat`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_repo_serializer.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from usan_api.compat.chat_serializer import serialize_chat
from usan_api.compat.serialization import pack_dynamic_vars
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession
from usan_api.repositories import chats as chats_repo


@pytest.mark.asyncio
async def test_next_seq_and_add_message(app_session, set_tenant_context):
    async with app_session() as db:
        await set_tenant_context(db)
        profile_id = (await db.execute(text("SELECT id FROM agent_profiles LIMIT 1"))).scalar_one()
        s = await chats_repo.add_session(
            db, agent_profile_id=profile_id, agent_version=1, dynamic_vars={}
        )
        await db.flush()
        assert await chats_repo.next_seq(db, s.id) == 1
        await chats_repo.add_message(db, session_id=s.id, seq=1, role="user", content="hi")
        await db.flush()
        assert await chats_repo.next_seq(db, s.id) == 2
        await db.rollback()


def test_serialize_chat_full_includes_transcript_and_messages():
    sid = uuid.uuid4()
    session = ChatSession(
        id=sid,
        agent_profile_id=uuid.uuid4(),
        agent_version=3,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars=pack_dynamic_vars({"name": "Pat"}, {"crm": 1}),
    )
    session.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.ended_at = None
    msgs = [
        ChatMessage(id=uuid.uuid4(), chat_session_id=sid, seq=1, role="user", content="hi"),
        ChatMessage(id=uuid.uuid4(), chat_session_id=sid, seq=2, role="agent", content="hello"),
    ]
    for m in msgs:
        m.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    out = serialize_chat(session, msgs, include_transcript=True).model_dump(exclude_none=True)
    assert out["chat_status"] == "ongoing"
    assert out["version"] == 3
    assert out["retell_llm_dynamic_variables"] == {"name": "Pat"}
    assert out["metadata"] == {"crm": 1}
    assert [m["role"] for m in out["message_with_tool_calls"]] == ["user", "agent"]
    assert out["message_with_tool_calls"][0]["message_id"].startswith("message_")
    assert "transcript" in out


def test_serialize_chat_list_item_omits_transcript_and_messages():
    sid = uuid.uuid4()
    session = ChatSession(
        id=sid, agent_profile_id=uuid.uuid4(), agent_version=1,
        status=ChatStatus.ONGOING, chat_type="api_chat", dynamic_vars={},
    )
    session.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.ended_at = None
    out = serialize_chat(session, [], include_transcript=False).model_dump(exclude_none=True)
    assert "transcript" not in out
    assert "message_with_tool_calls" not in out
    assert "retell_llm_dynamic_variables" not in out  # empty → omitted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_repo_serializer.py -v`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write the serializer `compat/chat_serializer.py`**

```python
"""Assemble the RetellAI ChatResponse object from native chat rows (Phase 4a)."""

from __future__ import annotations

from usan_api.compat import ids
from usan_api.compat.schemas.chats import CompatChat, CompatChatMessage
from usan_api.compat.serialization import to_ms, unpack_dynamic_vars
from usan_api.db.models import ChatMessage, ChatSession


def _line(message: ChatMessage) -> str:
    return f"{message.role.capitalize()}: {message.content}"


def serialize_chat(
    session: ChatSession,
    messages: list[ChatMessage],
    *,
    include_transcript: bool,
) -> CompatChat:
    """Build the RetellAI ChatResponse. include_transcript=False on the list path so
    transcript + message_with_tool_calls are omitted (V3ChatResponse forbids those keys)."""
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
    )
```

- [ ] **Step 4: Write the repository `repositories/chats.py`**

```python
"""Chat session/message persistence (Phase 4a). RLS-scoped; org_id auto-filled by DB default."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chats import ListChatsRequest
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession


async def add_session(
    db: AsyncSession,
    *,
    agent_profile_id: uuid.UUID,
    agent_version: int,
    dynamic_vars: dict[str, Any],
) -> ChatSession:
    session = ChatSession(
        agent_profile_id=agent_profile_id,
        agent_version=agent_version,
        status=ChatStatus.ONGOING,
        chat_type="api_chat",
        dynamic_vars=dynamic_vars,
    )
    db.add(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    session = await db.get(ChatSession, session_id)
    if session is None or session.archived_at is not None:
        return None
    return session


async def lock_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    """Load the session FOR UPDATE so concurrent completions serialize (seq safety)."""
    session = await db.get(ChatSession, session_id, with_for_update=True)
    if session is None or session.archived_at is not None:
        return None
    return session


async def next_seq(db: AsyncSession, session_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(ChatMessage.seq), 0) + 1).where(
        ChatMessage.chat_session_id == session_id
    )
    return int((await db.execute(stmt)).scalar_one())


async def add_message(
    db: AsyncSession, *, session_id: uuid.UUID, seq: int, role: str, content: str
) -> ChatMessage:
    message = ChatMessage(chat_session_id=session_id, seq=seq, role=role, content=content)
    db.add(message)
    return message


async def list_messages(db: AsyncSession, session_id: uuid.UUID) -> list[ChatMessage]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.chat_session_id == session_id)
        .order_by(ChatMessage.seq.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _base_query(body: ListChatsRequest):
    stmt = select(ChatSession).where(ChatSession.archived_at.is_(None))
    fc = body.filter_criteria or {}
    agent = fc.get("agent_id")
    if isinstance(agent, str) and agent:
        try:
            stmt = stmt.where(ChatSession.agent_profile_id == ids.decode_agent_id(agent))
        except CompatError:
            stmt = stmt.where(ChatSession.id == uuid.UUID(int=0))  # matches nothing
    status = fc.get("chat_status")
    if isinstance(status, str) and status in {s.value for s in ChatStatus}:
        stmt = stmt.where(ChatSession.status == ChatStatus(status))
    return stmt


async def query_sessions(db: AsyncSession, body: ListChatsRequest) -> list[ChatSession]:
    stmt = _base_query(body)
    descending = body.sort_order != "ascending"
    if body.pagination_key:
        try:
            cursor = await get_session(db, ids.decode_chat_id(body.pagination_key))
        except CompatError:
            cursor = None
        if cursor is not None:
            if descending:
                stmt = stmt.where(
                    or_(
                        ChatSession.started_at < cursor.started_at,
                        and_(ChatSession.started_at == cursor.started_at, ChatSession.id < cursor.id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        ChatSession.started_at > cursor.started_at,
                        and_(ChatSession.started_at == cursor.started_at, ChatSession.id > cursor.id),
                    )
                )
    if descending:
        stmt = stmt.order_by(ChatSession.started_at.desc(), ChatSession.id.desc())
    else:
        stmt = stmt.order_by(ChatSession.started_at.asc(), ChatSession.id.asc())
    if body.skip:
        stmt = stmt.offset(body.skip)
    stmt = stmt.limit(body.limit)
    return list((await db.execute(stmt)).scalars().all())


async def count_sessions(db: AsyncSession, body: ListChatsRequest) -> int:
    inner = _base_query(body).subquery()
    return int((await db.execute(select(func.count()).select_from(inner))).scalar_one())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_repo_serializer.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/repositories/chats.py apps/api/src/usan_api/compat/chat_serializer.py apps/api/tests/compat/test_chat_repo_serializer.py
git commit -m "feat(api): chat repository + ChatResponse serializer"
```

---

### Task 5: chat service layer (incl. the live Vertex turn)

**Files:**
- Create: `apps/api/src/usan_api/compat/chat_service.py`
- Test: `apps/api/tests/compat/test_chat_service.py`

**Interfaces:**
- Consumes: Task 2 ids, Task 3 schemas, Task 4 repo + serializer, `serialization.{pack_dynamic_vars, unpack_dynamic_vars, RESERVED_VAR_PREFIX}`, `agent_profiles_repo.{is_live_profile, get_profile, get_published_config}`, `schemas.agent_config.AgentConfig`, `prompt_substitution.{build_vars, substitute}`, `vertex_test.run_vertex_turn`.
- Produces: `create_chat(db, body) -> ChatSession`; `get_chat(db, chat_id) -> ChatSession`; `create_chat_completion(db, settings, body) -> list[ChatMessage]`; `update_chat(db, chat_id, body) -> ChatSession`; `end_chat(db, chat_id) -> None`; `delete_chat(db, chat_id) -> None`; `list_chats(db, body) -> tuple[list[ChatSession], str | None, bool, int | None]`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_service.py
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from usan_api.compat import chat_service
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_chat_id
from usan_api.compat.schemas.chats import CreateChatCompletionRequest, CreateChatRequest
from usan_api.settings import get_settings


async def _seed_chat(db, agent_token) -> str:
    body = CreateChatRequest(agent_id=agent_token)
    session = await chat_service.create_chat(db, body)
    return encode_chat_id(session.id)


@pytest.mark.asyncio
async def test_create_chat_rejects_unpublished_agent(app_session, set_tenant_context):
    async with app_session() as db:
        await set_tenant_context(db)
        with pytest.raises(CompatError) as exc:
            await chat_service.create_chat(db, CreateChatRequest(agent_id="agent_" + "0" * 32))
        assert exc.value.status_code == 422
        await db.rollback()


@pytest.mark.asyncio
async def test_completion_503_when_gcp_unset_persists_nothing(
    app_session, set_tenant_context, published_agent_token, monkeypatch
):
    spy = AsyncMock()
    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", spy)
    settings_no_gcp = get_settings().model_copy(update={"gcp_project": None})
    async with app_session() as db:
        await set_tenant_context(db)
        chat_id = await _seed_chat(db, published_agent_token)
        body = CreateChatCompletionRequest(chat_id=chat_id, content="hi")
        with pytest.raises(CompatError) as exc:
            await chat_service.create_chat_completion(db, settings_no_gcp, body)
        assert exc.value.status_code == 503
        spy.assert_not_awaited()
        # no agent message persisted
        n = (await db.execute(text("SELECT count(*) FROM chat_messages"))).scalar_one()
        assert n == 0
        await db.rollback()


@pytest.mark.asyncio
async def test_completion_returns_only_new_agent_message(
    app_session, set_tenant_context, published_agent_token, monkeypatch
):
    from usan_api.vertex_test import VertexTurn

    async def fake_turn(**kwargs):
        assert kwargs["tools"] == []
        # the prior user turn must be present as a genai "user" content
        assert kwargs["contents"][-1]["role"] == "user"
        return VertexTurn(text="hello there")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", fake_turn)
    settings_gcp = get_settings().model_copy(update={"gcp_project": "test-project"})
    async with app_session() as db:
        await set_tenant_context(db)
        chat_id = await _seed_chat(db, published_agent_token)
        new = await chat_service.create_chat_completion(
            db, settings_gcp, CreateChatCompletionRequest(chat_id=chat_id, content="hi")
        )
        assert [m.role for m in new] == ["agent"]
        assert new[0].content == "hello there"
        await db.rollback()
```

> Add a `published_agent_token` fixture to `tests/compat/conftest.py` returning a published compat `agent_id` string — reuse `_published_agent_id(compat_client, compat_headers)` (the same helper `web_agent_id` uses). It needs `compat_client`/`compat_headers`, so depend on those. The implementer wires it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_service.py -v`
Expected: FAIL — `ModuleNotFoundError: usan_api.compat.chat_service`.

- [ ] **Step 3: Write `compat/chat_service.py`**

```python
"""Compat chat-session service layer (Phase 4a).

create_chat_completion runs ONE Vertex text turn (text-only, tools=[]) reusing the
in-apps/api Vertex path — no LiveKit, no services/agent import. PHI/secret-safe: caught
exceptions re-raise as CompatError from None; the global handler logs type(exc).__name__.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chats import (
    CreateChatCompletionRequest,
    CreateChatRequest,
    ListChatsRequest,
    UpdateChatRequest,
)
from usan_api.compat.serialization import (
    RESERVED_VAR_PREFIX,
    pack_dynamic_vars,
    unpack_dynamic_vars,
)
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatMessage, ChatSession
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import chats as chats_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn


def _reject_reserved(vars_: dict[str, str] | None) -> None:
    if any(str(k).startswith(RESERVED_VAR_PREFIX) for k in (vars_ or {})):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")


async def create_chat(db: AsyncSession, body: CreateChatRequest) -> ChatSession:
    profile_id = ids.decode_agent_id(body.agent_id)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent_id must reference a published agent")
    _reject_reserved(body.retell_llm_dynamic_variables)

    profile = await agent_profiles_repo.get_profile(db, profile_id)
    assert profile is not None and profile.published_version is not None  # is_live_profile guaranteed
    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    session = await chats_repo.add_session(
        db,
        agent_profile_id=profile_id,
        agent_version=profile.published_version,
        dynamic_vars=packed,
    )
    await db.commit()
    return session


async def get_chat(db: AsyncSession, chat_id: str) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    return session


async def _load_published_config(db: AsyncSession, profile_id: uuid.UUID) -> AgentConfig:
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    version = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if version is None:
        raise CompatError(422, "agent is not available")
    return AgentConfig.model_validate(version.config)


async def create_chat_completion(
    db: AsyncSession, settings: Settings, body: CreateChatCompletionRequest
) -> list[ChatMessage]:
    # 1) lock the session row (serializes concurrent completions → safe seq)
    session = await chats_repo.lock_session(db, ids.decode_chat_id(body.chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    # 2) gate on status
    if session.status is not ChatStatus.ONGOING:
        raise CompatError(422, "chat is not ongoing")
    # 3) Vertex config gate — BEFORE any write
    if not settings.gcp_project:
        raise CompatError(503, "chat completion unavailable")

    try:
        # 4) persist the user turn
        user_seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=user_seq, role="user", content=body.content
        )
        await db.flush()
        # 5) build the system prompt from the published config + bare dynamic vars
        cfg = await _load_published_config(db, session.agent_profile_id)
        bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
        values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
        system_instruction = substitute(cfg.prompts.system_prompt, values)
        # 6) multi-turn contents (agent → genai "model"; user → "user")
        history = await chats_repo.list_messages(db, session.id)
        contents = [
            {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
            for m in history
        ]
        # 7) one text-only Vertex turn
        turn = await run_vertex_turn(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            system_instruction=system_instruction,
            tools=[],
            contents=contents,
            settings=settings,
        )
    except CompatError:
        raise
    except Exception as exc:
        # PHI/secret-safe: type name only; discard the whole uncommitted txn (no partial PHI).
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("chat completion failed")
        raise CompatError(502, "chat completion failed") from None

    # 8) persist the agent turn, commit, return ONLY the new agent message(s)
    agent_seq = await chats_repo.next_seq(db, session.id)
    agent_msg = await chats_repo.add_message(
        db, session_id=session.id, seq=agent_seq, role="agent", content=turn.text
    )
    await db.commit()
    return [agent_msg]


async def update_chat(db: AsyncSession, chat_id: str, body: UpdateChatRequest) -> ChatSession:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    _reject_reserved(body.override_dynamic_variables)
    bare_vars, metadata = unpack_dynamic_vars(session.dynamic_vars)
    if body.override_dynamic_variables is not None:
        bare_vars = body.override_dynamic_variables
    if body.metadata is not None:
        metadata = body.metadata
    session.dynamic_vars = pack_dynamic_vars(bare_vars, metadata)
    if body.custom_attributes is not None:
        session.custom_attributes = body.custom_attributes
    # data_storage_setting is accepted-and-ignored (4a).
    await db.commit()
    await db.refresh(session)
    return session


async def end_chat(db: AsyncSession, chat_id: str) -> None:
    session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    if session is None:
        raise CompatError(404, "chat not found")
    session.status = ChatStatus.ENDED
    session.ended_at = datetime.now(UTC)
    await db.commit()


async def delete_chat(db: AsyncSession, chat_id: str) -> None:
    try:
        session = await chats_repo.get_session(db, ids.decode_chat_id(chat_id))
    except CompatError:
        raise CompatError(404, "chat not found") from None
    if session is None:
        raise CompatError(404, "chat not found")
    session.archived_at = func.now()
    await db.commit()


async def list_chats(
    db: AsyncSession, body: ListChatsRequest
) -> tuple[list[ChatSession], str | None, bool, int | None]:
    sessions = await chats_repo.query_sessions(db, body)
    pagination_key = ids.encode_chat_id(sessions[-1].id) if sessions else None
    has_more = len(sessions) == body.limit
    total = await chats_repo.count_sessions(db, body) if body.include_total else None
    return sessions, pagination_key, has_more, total
```

> **`get_published_config` confirmation:** `_resolved_from_profile` (`repositories/agent_profiles.py:413-450`) calls `get_published_config(db, profile)` → a version object with `.config` (dict) + `.version`. If the public name differs, the implementer uses the same call `_resolved_from_profile` uses to obtain the published `AgentConfig`, and may instead call a higher-level resolver if one exists. The behavior required: load the **published** config of the chat's `agent_profile_id`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_chat_service.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/compat/chat_service.py apps/api/tests/compat/test_chat_service.py
git commit -m "feat(api): compat chat service (live Vertex completion, 503-first, 502 rollback)"
```

---

### Task 6: router + de-stub + freeze suite

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/chats.py`
- Modify: `apps/api/src/usan_api/compat/app.py`
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py`
- Modify: `apps/api/tests/test_compat_fidelity.py`
- Create: `apps/api/tests/compat/test_freeze_chats.py`

**Interfaces:**
- Consumes: Task 5 `chat_service`, Task 4 serializer/repo, Task 3 schemas, `get_compat_db`, `get_settings`, `serialization.to_ms`.
- Produces: the 7 mounted routes; `_UNSUPPORTED` no longer lists the 7 api_chat ops.

- [ ] **Step 1: Write the router `compat/routers/chats.py`**

```python
"""RetellAI-compatible chat (api_chat) endpoints (Phase 4a):

  POST   /create-chat              (201)
  POST   /create-chat-completion   (201)
  GET    /get-chat/{chat_id}
  POST   /v3/list-chats
  PATCH  /update-chat/{chat_id}
  PATCH  /end-chat/{chat_id}        (204)
  DELETE /delete-chat/{chat_id}     (204)

Auth + org-scoped RLS via get_compat_db. Each op emits a PHI-free audit line.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import chat_serializer, chat_service, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.chats import (
    CompatChat,
    CompatChatCompletion,
    CompatChatMessage,
    CreateChatCompletionRequest,
    CreateChatRequest,
    ListChatsRequest,
    ListChatsResponse,
    UpdateChatRequest,
)
from usan_api.compat.serialization import to_ms
from usan_api.repositories import chats as chats_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-chats"])


def _audit(request: Request, op: str, chat_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, chat_id=chat_id).info("compat chat op={op}")


async def _serialize_full(db: AsyncSession, session) -> CompatChat:
    messages = await chats_repo.list_messages(db, session.id)
    return chat_serializer.serialize_chat(session, messages, include_transcript=True)


@router.post(
    "/create-chat",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def create_chat(
    body: CreateChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.create_chat(db, body)
    _audit(request, "create-chat", ids.encode_chat_id(session.id))
    return await _serialize_full(db, session)


@router.post(
    "/create-chat-completion",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChatCompletion,
    response_model_exclude_none=True,
)
async def create_chat_completion(
    body: CreateChatCompletionRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChatCompletion:
    new_messages = await chat_service.create_chat_completion(db, settings, body)
    _audit(request, "create-chat-completion", body.chat_id)
    return CompatChatCompletion(
        messages=[
            CompatChatMessage(
                role=m.role,
                content=m.content,
                message_id=ids.encode_message_id(m.id),
                created_timestamp=to_ms(m.created_at) or 0,
            )
            for m in new_messages
        ]
    )


@router.get("/get-chat/{chat_id}", response_model=CompatChat, response_model_exclude_none=True)
async def get_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.get_chat(db, chat_id)
    _audit(request, "get-chat", chat_id)
    return await _serialize_full(db, session)


@router.post("/v3/list-chats", response_model=ListChatsResponse, response_model_exclude_none=True)
async def list_chats(
    body: ListChatsRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> ListChatsResponse:
    sessions, pagination_key, has_more, total = await chat_service.list_chats(db, body)
    _audit(request, "list-chats")
    items = [chat_serializer.serialize_chat(s, [], include_transcript=False) for s in sessions]
    return ListChatsResponse(
        items=items, pagination_key=pagination_key, has_more=has_more, total=total
    )


@router.patch("/update-chat/{chat_id}", response_model=CompatChat, response_model_exclude_none=True)
async def update_chat(
    chat_id: str,
    body: UpdateChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.update_chat(db, chat_id, body)
    _audit(request, "update-chat", chat_id)
    return await _serialize_full(db, session)


@router.patch("/end-chat/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_service.end_chat(db, chat_id)
    _audit(request, "end-chat", chat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/delete-chat/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_service.delete_chat(db, chat_id)
    _audit(request, "delete-chat", chat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 2: Register the router in `compat/app.py`**

Add the import alongside the others and the `include_router` call inside `build_compat_app`:

```python
from usan_api.compat.routers import chats as compat_chats
# ... with the other include_router calls:
    app.include_router(compat_chats.router)
```

- [ ] **Step 3: Remove the 7 api_chat entries from `unsupported.py`**

Delete these exact lines from `_UNSUPPORTED` (the `# --- Chat ---` block) — **keep** `create-sms-chat`:

```
    ("POST", "/create-chat"),
    ("POST", "/create-chat-completion"),
    ("GET", "/get-chat/{chat_id}"),
    ("POST", "/v3/list-chats"),
    ("DELETE", "/delete-chat/{chat_id}"),
    ("PATCH", "/end-chat/{chat_id}"),
    ("PATCH", "/update-chat/{chat_id}"),
```

Leave `("POST", "/create-sms-chat")`, all `*-chat-agent` lines, and `("PUT", "/rerun-chat-analysis/{chat_id}")` in place.

- [ ] **Step 4: Remove `("post","/create-chat")` from the fidelity parametrize**

In `apps/api/tests/test_compat_fidelity.py`, delete the line `("post", "/create-chat"),` from the `test_out_of_scope_returns_501_envelope` parametrize list. (It is the only chat entry in that list.)

- [ ] **Step 5: Write the freeze suite `tests/compat/test_freeze_chats.py`**

```python
"""Contract freeze for the api_chat chat-session surface (RetellAI parity Phase 4a)."""

from __future__ import annotations

import pytest

from usan_api.vertex_test import VertexTurn

from .conformance import assert_conforms, assert_sdk_roundtrip


@pytest.fixture
def mock_vertex(monkeypatch):
    """Stub the Vertex turn so the freeze suite places no real LLM call."""
    async def _fake(**kwargs):
        assert kwargs["tools"] == []
        return VertexTurn(text="hello from the agent")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _fake)


def _create_chat(client, headers, agent_id, **overrides):
    body = {"agent_id": agent_id}
    body.update(overrides)
    return client.post("/create-chat", json=body, headers=headers)


def test_create_chat_requires_key(compat_client, web_agent_id):
    r = compat_client.post("/create-chat", json={"agent_id": web_agent_id})
    assert r.status_code == 401


def test_create_chat_conforms(compat_client, compat_headers, web_agent_id):
    r = _create_chat(
        compat_client, compat_headers, web_agent_id,
        metadata={"crm": "x"}, retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "api_chat"
    assert isinstance(body["chat_id"], str) and body["chat_id"].startswith("chat_")
    assert isinstance(body["agent_id"], str)
    assert isinstance(body["agent_version"], int)
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")


def test_create_chat_rejects_malformed_agent(compat_client, compat_headers):
    assert _create_chat(compat_client, compat_headers, "not-an-agent").status_code == 422


def test_create_chat_rejects_unpublished_agent(compat_client, compat_headers):
    assert _create_chat(compat_client, compat_headers, "agent_" + "0" * 32).status_code == 422


def test_create_chat_rejects_reserved_prefix(compat_client, compat_headers, web_agent_id):
    r = _create_chat(
        compat_client, compat_headers, web_agent_id,
        retell_llm_dynamic_variables={"__meta__x": "1"},
    )
    assert r.status_code == 422


def test_completion_conforms_and_returns_agent_message(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_set
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [m["role"] for m in body["messages"]] == ["agent"]
    assert body["messages"][0]["content"] == "hello from the agent"
    for m in body["messages"]:
        assert_conforms(m, "Message")
    assert_sdk_roundtrip(body, "retell.types:ChatCreateChatCompletionResponse")


def test_completion_503_when_gcp_unset(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_unset
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 503


def test_get_chat_includes_transcript(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_set
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    got = compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).json()
    assert got["message_with_tool_calls"]
    assert "transcript" in got
    assert_conforms(got, "ChatResponse")
    assert_sdk_roundtrip(got, "retell.types:ChatResponse")


def test_end_then_completion_is_422(
    compat_client, compat_headers, web_agent_id, gcp_project_set, mock_vertex
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    assert compat_client.patch(f"/end-chat/{chat['chat_id']}", headers=compat_headers).status_code == 204
    got = compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).json()
    assert got["chat_status"] == "ended"
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "again"},
        headers=compat_headers,
    )
    assert r.status_code == 422


def test_delete_then_get_is_404(compat_client, compat_headers, web_agent_id):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    assert compat_client.delete(f"/delete-chat/{chat['chat_id']}", headers=compat_headers).status_code == 204
    assert compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).status_code == 404


def test_list_chats_items_omit_transcript_and_conform(compat_client, compat_headers, web_agent_id):
    _create_chat(compat_client, compat_headers, web_agent_id, retell_llm_dynamic_variables={"name": "Pat"})
    r = compat_client.post(
        "/v3/list-chats", json={"limit": 10, "include_total": True}, headers=compat_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["items"], list) and body["items"]
    for item in body["items"]:
        assert "transcript" not in item
        assert "message_with_tool_calls" not in item
        assert_conforms(item, "V3ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatListResponse")


def test_update_chat_round_trips_metadata(compat_client, compat_headers, web_agent_id):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    upd = compat_client.patch(
        f"/update-chat/{chat['chat_id']}",
        json={"metadata": {"crm": "y"}, "override_dynamic_variables": {"name": "Bo"}},
        headers=compat_headers,
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["metadata"] == {"crm": "y"}
    assert body["retell_llm_dynamic_variables"] == {"name": "Bo"}
    assert_conforms(body, "ChatResponse")
```

> **`gcp_project_set` / `gcp_project_unset` fixtures:** `create-chat-completion` reads `settings.gcp_project` at request time via `Depends(get_settings)`. Setting an env var won't affect the request unless `get_settings` is re-resolved. Add two fixtures to `tests/compat/conftest.py` that override the compat app's `get_settings` dependency (the same `dependency_overrides` mechanism `compat_client` uses for `get_compat_db`) to return `get_settings().model_copy(update={"gcp_project": "test-project"})` and `{"gcp_project": None}` respectively, restoring on teardown. `web_agent_id`/`compat_client`/`compat_headers` already exist.

- [ ] **Step 6: Run the FULL suite to verify served + both gate files green**

Run: `cd apps/api && uv run pytest tests/compat/test_freeze_chats.py tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -v`
Then the whole suite: `cd apps/api && uv run pytest`
Expected: PASS. `test_every_oracle_op_is_served_or_501_or_known_gap` and `test_501_stub_paths_match_oracle_exactly` stay green (the 7 routes are now served; `create-sms-chat`/`rerun-chat-analysis`/chat-agent stay 501); `test_out_of_scope_returns_501_envelope` no longer includes `/create-chat`.

- [ ] **Step 7: Lint + commit**

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/api/src/usan_api/compat/routers/chats.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/test_compat_fidelity.py apps/api/tests/compat/test_freeze_chats.py
git commit -m "feat(api): serve the 7 api_chat compat routes (de-stub + freeze suite)"
```

---

### Task 7: conformance discovery map + deployment note

**Files:**
- Modify: `apps/api/tests/compat/conformance.py`
- Create: `docs/deployment/chat-sessions-vertex.md`

**Interfaces:** none (docs + comment only).

- [ ] **Step 1: Add the chat discovery entries to the `conformance.py` module docstring**

Extend the "Discovered oracle component / SDK model names" block with:

```
  Chat:        oracle='ChatResponse'             sdk='retell.types:ChatResponse'
  ChatList:    oracle='V3ChatResponse' (items)   sdk='retell.types:ChatListResponse'
  Completion:  oracle='Message' (per messages[]) sdk='retell.types:ChatCreateChatCompletionResponse'
```

- [ ] **Step 2: Write `docs/deployment/chat-sessions-vertex.md`**

```markdown
# Deploying Phase 4a — Chat (api_chat) sessions

Phase 4a serves 7 RetellAI `api_chat` operations from the compat sub-app. It is **inert**
until deployed.

## Operator checklist

1. **Migration 0042** (`chat_sessions`, `chat_messages`, the `chat_status` enum) is owner-DDL —
   it runs as the `usan` table owner before `compose up` (same path as 0041). Verify
   `alembic heads` is a single `0042` after deploy.
2. **`GCP_PROJECT`** must be set in the API service env for `create-chat-completion` to run the
   live Vertex turn. Without it the endpoint returns **503** ("chat completion unavailable");
   the other six ops (create/get/list/update/end/delete) work without it. Vertex auth is ADC
   (the attached VM service account) — never the Gemini Developer API.
3. **No compat master flag.** Every chat op returns **401** until a super-admin mints a compat
   key (`/compat-keys`).

## Deviations from RetellAI (4a)

- `chat_type` is always `api_chat` (SMS → 4b).
- No `chat_analysis` / `collected_dynamic_variables` / `chat_cost` / `custom_attributes` in
  responses (no post-chat analysis yet); `rerun-chat-analysis` returns 501.
- Agent replies are `role=agent` text only (no tool-call/transition message roles).
- `/v3/list-chats` rich filters (sentiment, success, cost/duration ranges, custom fields) are
  accepted-but-not-honored; only `agent` + `chat_status` filter. `data_storage_setting` on
  update-chat is accepted-and-ignored.
- `create-chat-completion` is synchronous (one Vertex turn); the reply is one completed agent
  message (shape-conformant).
```

- [ ] **Step 3: Run the full suite once more + lint**

Run: `cd apps/api && uv run pytest && uv run ruff check . && uv run mypy`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/api/tests/compat/conformance.py docs/deployment/chat-sessions-vertex.md
git commit -m "docs(api): chat conformance discovery map + Phase 4a deployment note"
```

---

## Execution Notes

- BASE for each task's review-package = the commit recorded **before** dispatching that task's implementer (Task 1 BASE = the plan commit).
- Both 501 gate files (`tests/compat/test_surface_coverage.py` + `tests/test_compat_fidelity.py`) must end green together; run the FULL `apps/api` suite, not just `tests/compat`.
- Final whole-branch review on the most capable model; then `superpowers:finishing-a-development-branch` → push + open PR; squash-merge to `main` on the user's explicit go-ahead. **No `v*` tag.**
- The live LLM is mocked in every test (`monkeypatch` `usan_api.compat.chat_service.run_vertex_turn`) — the suite places no real Vertex call.
