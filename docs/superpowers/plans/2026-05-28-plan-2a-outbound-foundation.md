# USAN Voice Engine — Plan 2a: Outbound Call Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Place a real outbound call. `POST /v1/calls` (idempotent, DNC-gated) creates a `calls` row, generates a LiveKit room, dispatches the named agent worker, and dials the elder over the Telnyx SIP outbound trunk; the agent waits for the callee to answer and speaks the greeting. Ships with a persistent data layer (SQLAlchemy 2.x async + Alembic) plus elder and DNC management endpoints.

**Architecture:** Add a SQLAlchemy 2.x async data layer (asyncpg driver) with Alembic migrations to `apps/api`. New REST routers for elders, DNC, and calls. A `LiveKitDispatcher` uses the `livekit-api` server SDK to create an agent dispatch and a SIP outbound participant. The agent worker gains a name (so it can be dispatched explicitly), reads job metadata, and waits for the callee before greeting. Inbound (Plan 1) keeps working via the dispatch rule, now updated to reference the named worker.

**Tech Stack:** SQLAlchemy 2.x (async, `asyncpg`), Alembic (async env), `livekit-api`, FastAPI routers + dependency injection, Pydantic v2 schemas, `testcontainers[postgres]` for integration tests, pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` (§3 outbound flow, §4.1 REST surface, §5 data model, §5.4 idempotency).

---

## Scope

**In scope (Plan 2a):**
- Async DB layer + Alembic migrations; tables: `elders`, `dnc_list`, `calls` (+ `call_direction` / `call_status` enums).
- `POST /v1/elders`, `PUT /v1/elders/{id}`.
- `POST /v1/dnc`, `DELETE /v1/dnc/{phone}`.
- `POST /v1/calls` (idempotent enqueue + synchronous DNC gate + outbound dispatch), `GET /v1/calls/{call_id}`.
- LiveKit outbound dispatch: agent dispatch + SIP outbound participant via `livekit-api`.
- Agent worker: named dispatch, job-metadata parsing, `wait_for_participant()` before greeting.
- Infra: outbound SIP trunk config + named inbound dispatch rule + env + compose wiring + migrations-on-boot.

**Explicitly NOT in Plan 2a (deferred to Plan 2b):** voicemail detection, retry orchestrator, quiet-hour gating, Telnyx AMD. **Deferred to later plans:** `transcripts` / `wellness_logs` / `medication_logs` / `rag_chunks` tables, tool endpoints, recording/egress, observability. Plan 2a creates **only** the three tables it needs.

**Boundary discipline:** `apps/api` owns the database and dispatch; `services/agent` stays DB-free and receives per-call context via dispatch metadata. They do not import from each other.

---

## File structure produced by this plan

```
apps/api/
├── pyproject.toml                                  (modify: add deps)
├── alembic.ini                                     (create)
├── docker-entrypoint.sh                            (create)
├── Dockerfile                                      (modify: copy migrations + entrypoint)
├── migrations/
│   ├── env.py                                      (create — async)
│   ├── script.py.mako                              (create)
│   └── versions/
│       └── 0001_initial_schema.py                  (create)
├── src/usan_api/
│   ├── settings.py                                 (modify: async DB url + outbound fields)
│   ├── main.py                                     (modify: lifespan + routers)
│   ├── db/
│   │   ├── __init__.py                             (create)
│   │   ├── base.py                                 (create: Base + enums)
│   │   ├── models.py                               (create: Elder, DNCEntry, Call)
│   │   └── session.py                              (create: engine, factory, get_db)
│   ├── schemas/
│   │   ├── __init__.py                             (create)
│   │   ├── elder.py                                (create)
│   │   ├── dnc.py                                  (create)
│   │   └── call.py                                 (create)
│   ├── repositories/
│   │   ├── __init__.py                             (create)
│   │   ├── elders.py                               (create)
│   │   ├── dnc.py                                  (create)
│   │   └── calls.py                                (create)
│   ├── routers/
│   │   ├── __init__.py                             (create)
│   │   ├── elders.py                               (create)
│   │   ├── dnc.py                                  (create)
│   │   └── calls.py                                (create)
│   └── livekit_dispatch.py                         (create)
└── tests/
    ├── conftest.py                                 (modify: testcontainers + client override)
    ├── test_settings.py                            (modify: url + outbound field tests)
    ├── test_dnc.py                                 (create)
    ├── test_elders.py                              (create)
    ├── test_calls.py                               (create)
    └── test_livekit_dispatch.py                    (create)

services/agent/
├── src/usan_agent/
│   ├── settings.py                                 (modify: AGENT_NAME)
│   └── worker.py                                   (modify: named dispatch, metadata, wait)
└── tests/
    └── test_worker.py                              (create)

infra/
├── .env.example                                    (modify)
├── docker-compose.yml                              (modify: api/agent env)
├── livekit-sip-outbound-trunk.json                 (create)
├── livekit-sip-dispatch-rule.json                  (modify: name the agent)
└── README.md                                       (modify: outbound setup)
```

---

## Task 1: Add data-layer and LiveKit dependencies to `apps/api`

**Files:**
- Modify: `apps/api/pyproject.toml`

- [ ] **Step 1: Add runtime + dev dependencies**

In `apps/api/pyproject.toml`, replace the `dependencies` list and the `dev` group with:

```toml
dependencies = [
    "fastapi>=0.136.0",
    "uvicorn[standard]>=0.48.0",
    "pydantic>=2.13.0",
    "pydantic-settings>=2.14.0",
    "loguru>=0.7.3",
    "httpx>=0.28.1",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    "livekit-api>=1.0.0",
]

[dependency-groups]
dev = [
    "pytest>=9.0.0",
    "pytest-asyncio>=1.4.0",
    "ruff>=0.15.0",
    "mypy>=1.13.0",
    "testcontainers[postgres]>=4.8.0",
]
```

- [ ] **Step 2: Resolve and lock**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv sync
```

Expected: updates `uv.lock`, installs sqlalchemy, asyncpg, alembic, livekit-api, testcontainers. No resolution errors.

> **Note:** If `livekit-api>=1.0.0` does not resolve, pin to the latest `livekit-api` on PyPI at run time. Its `LiveKitAPI`, `CreateAgentDispatchRequest`, and `CreateSIPParticipantRequest` surface is what this plan depends on.

- [ ] **Step 3: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/pyproject.toml apps/api/uv.lock
git commit -m "feat(api): add sqlalchemy, asyncpg, alembic, livekit-api deps"
```

---

## Task 2: Extend API Settings (async DB URL + outbound fields)

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Modify: `apps/api/tests/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_settings.py`:

```python
def test_database_url_async_converts_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_database_url_async_leaves_asyncpg_untouched(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_livekit_http_url_converts_ws(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit:7880")

    s = Settings()

    assert s.livekit_http_url == "https://livekit:7880"


def test_outbound_fields_default_none_and_agent_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.delenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", raising=False)
    monkeypatch.delenv("TELNYX_CALLER_ID", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    s = Settings()

    assert s.livekit_sip_outbound_trunk_id is None
    assert s.telnyx_caller_id is None
    assert s.agent_name == "usan-agent"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_settings.py -v
```

Expected: the four new tests FAIL with `AttributeError` (no `database_url_async`, etc.).

- [ ] **Step 3: Add fields and properties to `Settings`**

In `apps/api/src/usan_api/settings.py`, add these fields inside the `Settings` class (after `log_level`):

```python
    livekit_sip_outbound_trunk_id: str | None = Field(
        default=None, alias="LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
    )
    telnyx_caller_id: str | None = Field(default=None, alias="TELNYX_CALLER_ID")
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
```

And add these two properties inside the `Settings` class (after the `_ws_scheme` validator):

```python
    @property
    def database_url_async(self) -> str:
        """DATABASE_URL with the asyncpg driver, for SQLAlchemy's async engine."""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @property
    def livekit_http_url(self) -> str:
        """LIVEKIT_URL as an http(s) URL, for the livekit-api server SDK."""
        url = self.livekit_url
        if url.startswith("wss://"):
            return "https://" + url[len("wss://") :]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://") :]
        return url
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_settings.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings.py
git commit -m "feat(api): add async DB url + outbound dispatch settings"
```

---

## Task 3: DB base, enums, and ORM models

**Files:**
- Create: `apps/api/src/usan_api/db/__init__.py`
- Create: `apps/api/src/usan_api/db/base.py`
- Create: `apps/api/src/usan_api/db/models.py`

- [ ] **Step 1: Create the package marker**

Create `apps/api/src/usan_api/db/__init__.py` (empty file).

- [ ] **Step 2: Write `db/base.py`**

Create `apps/api/src/usan_api/db/base.py`:

```python
import enum

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class CallDirection(enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class CallStatus(enum.Enum):
    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    VOICEMAIL_LEFT = "voicemail_left"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    DNC_BLOCKED = "dnc_blocked"
    CANCELLED = "cancelled"
```

- [ ] **Step 3: Write `db/models.py`**

Create `apps/api/src/usan_api/db/models.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, Text, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from usan_api.db.base import Base, CallDirection, CallStatus


def _enum_values(enum_cls: type) -> list[str]:
    """Store PG enum values (e.g. 'outbound'), not Python member names."""
    return [member.value for member in enum_cls]


class Elder(Base):
    __tablename__ = "elders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    external_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_voice: Mapped[str | None] = mapped_column(Text)
    # SQLAlchemy reserves the ``metadata`` attribute on Declarative classes, so
    # the Python attribute is ``meta`` while the column stays ``metadata``.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DNCEntry(Base):
    __tablename__ = "dnc_list"

    phone_e164: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id", ondelete="SET NULL")
    )
    direction: Mapped[CallDirection] = mapped_column(
        SAEnum(
            CallDirection,
            name="call_direction",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
    )
    status: Mapped[CallStatus] = mapped_column(
        SAEnum(
            CallStatus,
            name="call_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=CallStatus.QUEUED.value,
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    livekit_room: Mapped[str | None] = mapped_column(Text)
    sip_call_id: Mapped[str | None] = mapped_column(Text)
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    parent_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id")
    )
    attempt: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    end_reason: Mapped[str | None] = mapped_column(Text)
    recording_uri: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 4: Smoke test imports**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run python -c "from usan_api.db.models import Elder, DNCEntry, Call; from usan_api.db.base import CallStatus; print('ok', CallStatus.QUEUED.value)"
```

Expected: `ok queued`.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/db/__init__.py apps/api/src/usan_api/db/base.py apps/api/src/usan_api/db/models.py
git commit -m "feat(api): add SQLAlchemy Base, enums, and ORM models"
```

---

## Task 4: DB engine, session factory, and `get_db` dependency

**Files:**
- Create: `apps/api/src/usan_api/db/session.py`

- [ ] **Step 1: Write `db/session.py`**

Create `apps/api/src/usan_api/db/session.py`:

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from usan_api.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url_async, pool_pre_ping=True
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Handlers commit explicitly; this only rolls back and closes."""
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _factory = None
```

- [ ] **Step 2: Smoke test imports**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run python -c "from usan_api.db.session import get_db, get_engine, dispose_engine; print('ok')"
```

Expected: `ok` (no DB connection is made on import).

- [ ] **Step 3: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/db/session.py
git commit -m "feat(api): add async engine, session factory, and get_db dependency"
```

---

## Task 5: Alembic scaffolding (async)

**Files:**
- Create: `apps/api/alembic.ini`
- Create: `apps/api/migrations/env.py`
- Create: `apps/api/migrations/script.py.mako`

- [ ] **Step 1: Write `alembic.ini`**

Create `apps/api/alembic.ini`:

```ini
[alembic]
script_location = migrations
prepend_sys_path = src
path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Write `migrations/env.py`**

Create `apps/api/migrations/env.py`:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from usan_api.db import models  # noqa: F401  (import side effect: register tables)
from usan_api.db.base import Base
from usan_api.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url_async


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 3: Write `migrations/script.py.mako`**

Create `apps/api/migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Verify alembic can load its config**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
DATABASE_URL=postgresql://u:p@host/db LIVEKIT_API_KEY=key \
  LIVEKIT_API_SECRET=$(python -c "print('a'*32)") LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic heads
```

Expected: prints nothing useful yet (no revisions) but exits 0 with no traceback. If it errors importing `usan_api`, confirm `uv sync` installed the project package.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/alembic.ini apps/api/migrations/env.py apps/api/migrations/script.py.mako
git commit -m "feat(api): add async Alembic scaffolding"
```

---

## Task 6: Initial migration (extensions, enums, three tables)

**Files:**
- Create: `apps/api/migrations/versions/0001_initial_schema.py`

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0001_initial_schema.py`:

```python
"""initial schema: elders, dnc_list, calls

Revision ID: 0001
Revises:
Create Date: 2026-05-28

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE elders (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id     TEXT UNIQUE,
            name            TEXT NOT NULL,
            phone_e164      TEXT NOT NULL UNIQUE,
            timezone        TEXT NOT NULL,
            preferred_voice TEXT,
            metadata        JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE dnc_list (
            phone_e164  TEXT PRIMARY KEY,
            reason      TEXT,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("CREATE TYPE call_direction AS ENUM ('outbound', 'inbound')")
    op.execute(
        """
        CREATE TYPE call_status AS ENUM (
            'queued', 'dialing', 'ringing', 'in_progress',
            'completed', 'voicemail_left', 'no_answer',
            'busy', 'failed', 'dnc_blocked', 'cancelled'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE calls (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            elder_id           UUID REFERENCES elders(id) ON DELETE SET NULL,
            direction          call_direction NOT NULL,
            status             call_status NOT NULL DEFAULT 'queued',
            idempotency_key    TEXT UNIQUE,
            livekit_room       TEXT,
            sip_call_id        TEXT,
            dynamic_vars       JSONB NOT NULL DEFAULT '{}',
            parent_call_id     UUID REFERENCES calls(id),
            attempt            SMALLINT NOT NULL DEFAULT 1,
            scheduled_at       TIMESTAMPTZ,
            started_at         TIMESTAMPTZ,
            answered_at        TIMESTAMPTZ,
            ended_at           TIMESTAMPTZ,
            duration_seconds   INTEGER,
            end_reason         TEXT,
            recording_uri      TEXT,
            error              JSONB,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("CREATE INDEX idx_calls_elder ON calls(elder_id, created_at DESC)")
    op.execute(
        """
        CREATE INDEX idx_calls_status_scheduled ON calls(status, scheduled_at)
            WHERE status IN ('queued', 'no_answer', 'voicemail_left')
        """
    )
    op.execute("CREATE INDEX idx_calls_livekit_room ON calls(livekit_room)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calls")
    op.execute("DROP TYPE IF EXISTS call_status")
    op.execute("DROP TYPE IF EXISTS call_direction")
    op.execute("DROP TABLE IF EXISTS dnc_list")
    op.execute("DROP TABLE IF EXISTS elders")
```

> **Note:** `elders.phone_e164` is `UNIQUE`, which already creates an index, so the spec's `idx_elders_phone` is intentionally omitted as redundant. `pgcrypto` is left in place on downgrade (other migrations may rely on `gen_random_uuid()`).

- [ ] **Step 2: Apply against the running Plan 1 Postgres to verify**

The stack from Plan 1 should be up (`make up`). Run the migration against it:

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic upgrade head
```

Expected: `Running upgrade  -> 0001, initial schema`. Verify tables:

```bash
docker exec usan-postgres psql -U usan -d usan -c "\dt"
```

Expected: `calls`, `dnc_list`, `elders` listed.

- [ ] **Step 3: Verify downgrade works, then re-upgrade**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic downgrade base && \
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic upgrade head
```

Expected: clean downgrade to base then upgrade back to `0001`, no errors.

- [ ] **Step 4: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/migrations/versions/0001_initial_schema.py
git commit -m "feat(api): add initial migration for elders, dnc_list, calls"
```

---

## Task 7: Test fixtures (testcontainers Postgres + client override)

**Files:**
- Modify: `apps/api/tests/conftest.py`

This replaces the Plan 1 `client` fixture with one backed by a real Postgres (via testcontainers), migrated with Alembic, and with `get_db` overridden. All DB-backed tests run against this container. `get_settings.cache_clear()` is called so the per-test environment is picked up.

- [ ] **Step 1: Rewrite `apps/api/tests/conftest.py`**

Replace the entire contents of `apps/api/tests/conftest.py` with:

```python
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from usan_api.db.session import get_db
from usan_api.main import create_app
from usan_api.settings import get_settings

API_DIR = Path(__file__).resolve().parents[1]
TEST_SECRET = "a" * 32


@pytest.fixture(scope="session")
def database_url() -> str:
    with PostgresContainer(
        "pgvector/pgvector:pg18", username="usan", password="usan", dbname="usan"
    ) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        url = f"postgresql://usan:usan@{host}:{port}/usan"
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": TEST_SECRET,
            "LIVEKIT_URL": "ws://livekit:7880",
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env=env,
            check=True,
        )
        yield url


@pytest.fixture(scope="session")
def async_database_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _truncate(async_url: str) -> None:
    engine = create_async_engine(async_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE calls, dnc_list, elders RESTART IDENTITY CASCADE")
        )
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(async_database_url: str):
    yield
    asyncio.run(_truncate(async_database_url))


@pytest.fixture
def client(database_url: str, async_database_url: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("AGENT_NAME", "usan-agent")
    get_settings.cache_clear()

    test_engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        asyncio.run(test_engine.dispose())
        get_settings.cache_clear()
```

> **Note:** `NullPool` is used in tests so each asyncpg connection is created and closed within the request's event loop, avoiding cross-loop binding issues between `TestClient` requests and the per-test truncation.

- [ ] **Step 2: Confirm the existing health test still passes against the new fixture**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_health.py -v
```

Expected: PASS (first run pulls the `pgvector/pgvector:pg18` image and starts a container — may take a minute). Requires Docker running.

- [ ] **Step 3: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/tests/conftest.py
git commit -m "test(api): back tests with testcontainers Postgres + migrations"
```

---

## Task 8: DNC schema, repository, and router

**Files:**
- Create: `apps/api/src/usan_api/schemas/__init__.py`
- Create: `apps/api/src/usan_api/schemas/dnc.py`
- Create: `apps/api/src/usan_api/repositories/__init__.py`
- Create: `apps/api/src/usan_api/repositories/dnc.py`
- Create: `apps/api/src/usan_api/routers/__init__.py`
- Create: `apps/api/src/usan_api/routers/dnc.py`
- Create: `apps/api/tests/test_dnc.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_dnc.py`:

```python
def test_add_dnc_returns_201(client):
    r = client.post("/v1/dnc", json={"phone_e164": "+15550001111", "reason": "requested"})
    assert r.status_code == 201
    body = r.json()
    assert body["phone_e164"] == "+15550001111"
    assert body["reason"] == "requested"


def test_add_dnc_is_idempotent_upsert(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "a"})
    r = client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "b"})
    assert r.status_code == 201
    assert r.json()["reason"] == "b"


def test_remove_dnc_returns_204_then_404(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550003333", "reason": None})
    d1 = client.delete("/v1/dnc/%2B15550003333")
    assert d1.status_code == 204
    d2 = client.delete("/v1/dnc/%2B15550003333")
    assert d2.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_dnc.py -v
```

Expected: FAIL — 404 from FastAPI (route not registered yet).

- [ ] **Step 3: Create package markers and schema**

Create `apps/api/src/usan_api/schemas/__init__.py` (empty).
Create `apps/api/src/usan_api/repositories/__init__.py` (empty).
Create `apps/api/src/usan_api/routers/__init__.py` (empty).

Create `apps/api/src/usan_api/schemas/dnc.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field

from usan_api.db.models import DNCEntry


class DNCCreate(BaseModel):
    phone_e164: str = Field(min_length=1)
    reason: str | None = None


class DNCResponse(BaseModel):
    phone_e164: str
    reason: str | None
    added_at: datetime

    @classmethod
    def from_model(cls, entry: DNCEntry) -> "DNCResponse":
        return cls(
            phone_e164=entry.phone_e164, reason=entry.reason, added_at=entry.added_at
        )
```

- [ ] **Step 4: Write the repository**

Create `apps/api/src/usan_api/repositories/dnc.py`:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import DNCEntry


async def is_blocked(db: AsyncSession, phone_e164: str) -> bool:
    result = await db.execute(
        select(DNCEntry).where(DNCEntry.phone_e164 == phone_e164)
    )
    return result.scalar_one_or_none() is not None


async def add_entry(
    db: AsyncSession, phone_e164: str, reason: str | None
) -> DNCEntry:
    entry = await db.get(DNCEntry, phone_e164)
    if entry is None:
        entry = DNCEntry(phone_e164=phone_e164, reason=reason)
        db.add(entry)
    else:
        entry.reason = reason
    await db.flush()
    await db.refresh(entry)
    return entry


async def remove_entry(db: AsyncSession, phone_e164: str) -> bool:
    entry = await db.get(DNCEntry, phone_e164)
    if entry is None:
        return False
    await db.delete(entry)
    await db.flush()
    return True
```

- [ ] **Step 5: Write the router**

Create `apps/api/src/usan_api/routers/dnc.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.session import get_db
from usan_api.repositories import dnc as dnc_repo
from usan_api.schemas.dnc import DNCCreate, DNCResponse

router = APIRouter(prefix="/v1/dnc", tags=["dnc"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DNCResponse)
async def add_dnc(
    body: DNCCreate, db: AsyncSession = Depends(get_db)
) -> DNCResponse:
    entry = await dnc_repo.add_entry(db, body.phone_e164, body.reason)
    await db.commit()
    return DNCResponse.from_model(entry)


@router.delete("/{phone_e164}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dnc(phone_e164: str, db: AsyncSession = Depends(get_db)) -> None:
    removed = await dnc_repo.remove_entry(db, phone_e164)
    if not removed:
        raise HTTPException(status_code=404, detail="not on DNC list")
    await db.commit()
```

- [ ] **Step 6: Register the router in the app factory**

In `apps/api/src/usan_api/main.py`, add the import near the top:

```python
from usan_api.routers import dnc
```

and inside `create_app`, before `return app`, add:

```python
    app.include_router(dnc.router)
```

- [ ] **Step 7: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_dnc.py -v
```

Expected: 3 PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/schemas apps/api/src/usan_api/repositories apps/api/src/usan_api/routers apps/api/src/usan_api/main.py apps/api/tests/test_dnc.py
git commit -m "feat(api): add DNC list endpoints"
```

---

## Task 9: Elder schema, repository, and router

**Files:**
- Create: `apps/api/src/usan_api/schemas/elder.py`
- Create: `apps/api/src/usan_api/repositories/elders.py`
- Create: `apps/api/src/usan_api/routers/elders.py`
- Create: `apps/api/tests/test_elders.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_elders.py`:

```python
import uuid


def test_create_elder_returns_201(client):
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": "+15551112222",
            "timezone": "America/New_York",
            "metadata": {"floor": 3},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Ada"
    assert body["metadata"] == {"floor": 3}
    assert uuid.UUID(body["id"])


def test_create_elder_duplicate_phone_returns_409(client):
    client.post(
        "/v1/elders",
        json={"name": "A", "phone_e164": "+15553334444", "timezone": "UTC"},
    )
    r = client.post(
        "/v1/elders",
        json={"name": "B", "phone_e164": "+15553334444", "timezone": "UTC"},
    )
    assert r.status_code == 409


def test_update_elder_returns_200(client):
    created = client.post(
        "/v1/elders",
        json={"name": "A", "phone_e164": "+15555556666", "timezone": "UTC"},
    )
    elder_id = created.json()["id"]
    r = client.put(f"/v1/elders/{elder_id}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_update_missing_elder_returns_404(client):
    r = client.put(f"/v1/elders/{uuid.uuid4()}", json={"name": "X"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_elders.py -v
```

Expected: FAIL — route not registered.

- [ ] **Step 3: Write the schema**

Create `apps/api/src/usan_api/schemas/elder.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from usan_api.db.models import Elder


class ElderCreate(BaseModel):
    name: str = Field(min_length=1)
    phone_e164: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    external_id: str | None = None
    preferred_voice: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ElderUpdate(BaseModel):
    name: str | None = None
    phone_e164: str | None = None
    timezone: str | None = None
    external_id: str | None = None
    preferred_voice: str | None = None
    metadata: dict[str, Any] | None = None


class ElderResponse(BaseModel):
    id: uuid.UUID
    external_id: str | None
    name: str
    phone_e164: str
    timezone: str
    preferred_voice: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, elder: Elder) -> "ElderResponse":
        return cls(
            id=elder.id,
            external_id=elder.external_id,
            name=elder.name,
            phone_e164=elder.phone_e164,
            timezone=elder.timezone,
            preferred_voice=elder.preferred_voice,
            metadata=elder.meta,
            created_at=elder.created_at,
            updated_at=elder.updated_at,
        )
```

- [ ] **Step 4: Write the repository**

Create `apps/api/src/usan_api/repositories/elders.py`:

```python
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Elder


async def create_elder(
    db: AsyncSession,
    *,
    name: str,
    phone_e164: str,
    timezone: str,
    external_id: str | None = None,
    preferred_voice: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Elder:
    elder = Elder(
        name=name,
        phone_e164=phone_e164,
        timezone=timezone,
        external_id=external_id,
        preferred_voice=preferred_voice,
        meta=metadata or {},
    )
    db.add(elder)
    await db.flush()
    await db.refresh(elder)
    return elder


async def get_elder(db: AsyncSession, elder_id: uuid.UUID) -> Elder | None:
    return await db.get(Elder, elder_id)


async def update_elder(
    db: AsyncSession, elder_id: uuid.UUID, fields: dict[str, Any]
) -> Elder | None:
    elder = await db.get(Elder, elder_id)
    if elder is None:
        return None
    for key, value in fields.items():
        setattr(elder, "meta" if key == "metadata" else key, value)
    await db.flush()
    await db.refresh(elder)
    return elder
```

- [ ] **Step 5: Write the router**

Create `apps/api/src/usan_api/routers/elders.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.session import get_db
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.elder import ElderCreate, ElderResponse, ElderUpdate

router = APIRouter(prefix="/v1/elders", tags=["elders"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ElderResponse)
async def create_elder(
    body: ElderCreate, db: AsyncSession = Depends(get_db)
) -> ElderResponse:
    try:
        elder = await elders_repo.create_elder(
            db,
            name=body.name,
            phone_e164=body.phone_e164,
            timezone=body.timezone,
            external_id=body.external_id,
            preferred_voice=body.preferred_voice,
            metadata=body.metadata,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="elder with this phone_e164 or external_id already exists",
        ) from exc
    return ElderResponse.from_model(elder)


@router.put("/{elder_id}", response_model=ElderResponse)
async def update_elder(
    elder_id: uuid.UUID, body: ElderUpdate, db: AsyncSession = Depends(get_db)
) -> ElderResponse:
    elder = await elders_repo.update_elder(
        db, elder_id, body.model_dump(exclude_unset=True)
    )
    if elder is None:
        raise HTTPException(status_code=404, detail="elder not found")
    await db.commit()
    return ElderResponse.from_model(elder)
```

- [ ] **Step 6: Register the router**

In `apps/api/src/usan_api/main.py`, add the import:

```python
from usan_api.routers import elders
```

and inside `create_app`, before `return app`:

```python
    app.include_router(elders.router)
```

- [ ] **Step 7: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_elders.py -v
```

Expected: 4 PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/schemas/elder.py apps/api/src/usan_api/repositories/elders.py apps/api/src/usan_api/routers/elders.py apps/api/src/usan_api/main.py apps/api/tests/test_elders.py
git commit -m "feat(api): add elder create/update endpoints"
```

---

## Task 10: Call schema and repository

**Files:**
- Create: `apps/api/src/usan_api/schemas/call.py`
- Create: `apps/api/src/usan_api/repositories/calls.py`

This task has no endpoint yet (Task 12 adds the router). The repository is exercised through the call endpoints' tests; here we only smoke-test imports.

- [ ] **Step 1: Write the schema**

Create `apps/api/src/usan_api/schemas/call.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from usan_api.db.models import Call


class CreateCallRequest(BaseModel):
    elder_id: uuid.UUID
    idempotency_key: str = Field(min_length=1)
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)


class CallResponse(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID | None
    direction: str
    status: str
    idempotency_key: str | None
    livekit_room: str | None
    attempt: int
    recording_uri: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, call: Call) -> "CallResponse":
        return cls(
            id=call.id,
            elder_id=call.elder_id,
            direction=call.direction.value,
            status=call.status.value,
            idempotency_key=call.idempotency_key,
            livekit_room=call.livekit_room,
            attempt=call.attempt,
            recording_uri=call.recording_uri,
            created_at=call.created_at,
        )
```

- [ ] **Step 2: Write the repository**

Create `apps/api/src/usan_api/repositories/calls.py`:

```python
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Call, CallDirection, CallStatus


async def create_call(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    direction: CallDirection,
    status: CallStatus,
    idempotency_key: str | None = None,
    livekit_room: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    call = Call(
        elder_id=elder_id,
        direction=direction,
        status=status,
        idempotency_key=idempotency_key,
        livekit_room=livekit_room,
        dynamic_vars=dynamic_vars or {},
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    return call


async def get_call(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    return await db.get(Call, call_id)


async def get_by_idempotency_key(db: AsyncSession, key: str) -> Call | None:
    result = await db.execute(select(Call).where(Call.idempotency_key == key))
    return result.scalar_one_or_none()


async def set_status(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 3: Smoke test imports**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run python -c "from usan_api.repositories import calls; from usan_api.schemas.call import CallResponse, CreateCallRequest; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/schemas/call.py apps/api/src/usan_api/repositories/calls.py
git commit -m "feat(api): add call schema and repository"
```

---

## Task 11: LiveKit outbound dispatcher

**Files:**
- Create: `apps/api/src/usan_api/livekit_dispatch.py`
- Create: `apps/api/tests/test_livekit_dispatch.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_livekit_dispatch.py`:

```python
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_api import livekit_dispatch
from usan_api.db.models import Call, CallDirection, CallStatus, Elder
from usan_api.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_x",
        "TELNYX_CALLER_ID": "+15551230000",
    }
    base.update(overrides)
    return Settings(**base)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.mark.asyncio
async def test_dispatch_invokes_agent_and_sip(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda settings: fake)

    elder = Elder(name="Ada", phone_e164="+15551234567", timezone="UTC")
    call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        livekit_room="usan-outbound-abc",
        dynamic_vars={},
    )

    await livekit_dispatch.dispatch_outbound_call(
        call, elder=elder, settings=_settings()
    )

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    fake.sip.create_sip_participant.assert_awaited_once()
    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.sip_call_to == "+15551234567"
    assert sip_req.sip_trunk_id == "ST_x"
    assert sip_req.room_name == "usan-outbound-abc"


@pytest.mark.asyncio
async def test_dispatch_requires_outbound_config():
    elder = Elder(name="Ada", phone_e164="+15551234567", timezone="UTC")
    call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        livekit_room="r",
        dynamic_vars={},
    )
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)

    with pytest.raises(livekit_dispatch.OutboundDispatchError):
        await livekit_dispatch.dispatch_outbound_call(
            call, elder=elder, settings=settings
        )
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_livekit_dispatch.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.livekit_dispatch'`.

- [ ] **Step 3: Write the dispatcher**

Create `apps/api/src/usan_api/livekit_dispatch.py`:

```python
import json

from livekit import api
from loguru import logger

from usan_api.db.models import Call, Elder
from usan_api.settings import Settings


class OutboundDispatchError(Exception):
    """Raised when an outbound call cannot be dispatched (misconfig or upstream error)."""


def build_livekit_api(settings: Settings) -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_http_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


async def dispatch_outbound_call(
    call: Call, *, elder: Elder, settings: Settings
) -> None:
    if not settings.livekit_sip_outbound_trunk_id or not settings.telnyx_caller_id:
        raise OutboundDispatchError(
            "outbound calling not configured: set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID and TELNYX_CALLER_ID"
        )
    if not call.livekit_room:
        raise OutboundDispatchError("call has no livekit_room assigned")

    metadata = json.dumps(
        {
            "call_id": str(call.id),
            "direction": "outbound",
            "dynamic_vars": call.dynamic_vars,
        }
    )

    async with build_livekit_api(settings) as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=call.livekit_room,
                metadata=metadata,
            )
        )
        await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                sip_call_to=elder.phone_e164,
                sip_number=settings.telnyx_caller_id,
                room_name=call.livekit_room,
                participant_identity="callee",
                participant_name=elder.name,
                wait_until_answered=False,
                play_ringtone=True,
            )
        )

    logger.bind(call_id=str(call.id), room=call.livekit_room).info(
        "Dispatched agent + SIP participant for outbound call"
    )
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_livekit_dispatch.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/livekit_dispatch.py apps/api/tests/test_livekit_dispatch.py
git commit -m "feat(api): add LiveKit outbound dispatcher (agent dispatch + SIP)"
```

---

## Task 12: Calls router (`POST /v1/calls`, `GET /v1/calls/{id}`)

**Files:**
- Create: `apps/api/src/usan_api/routers/calls.py`
- Create: `apps/api/tests/test_calls.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_calls.py`:

```python
import uuid
from unittest.mock import AsyncMock

import pytest

from usan_api import livekit_dispatch


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={"name": "Ada", "phone_e164": "+15551234567", "timezone": "UTC"},
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture
def mock_dispatch(monkeypatch) -> AsyncMock:
    dispatch = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_outbound_call", dispatch)
    return dispatch


def test_enqueue_call_dispatches_and_returns_202(client, mock_dispatch):
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "k1", "dynamic_vars": {}},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["direction"] == "outbound"
    assert body["status"] == "dialing"
    mock_dispatch.assert_awaited_once()


def test_enqueue_call_idempotent_replay_returns_200(client, mock_dispatch):
    elder_id = _create_elder(client)
    r1 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
    )
    r2 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
    )
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    mock_dispatch.assert_awaited_once()


def test_enqueue_call_conflicting_idempotency_returns_409(client, mock_dispatch):
    elder_id = _create_elder(client)
    client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 1}},
    )
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 2}},
    )
    assert r.status_code == 409


def test_enqueue_call_dnc_blocked(client, mock_dispatch):
    elder_id = _create_elder(client)
    assert (
        client.post(
            "/v1/dnc", json={"phone_e164": "+15551234567", "reason": "test"}
        ).status_code
        == 201
    )
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "d1", "dynamic_vars": {}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dnc_blocked"
    mock_dispatch.assert_not_awaited()


def test_enqueue_call_unknown_elder_returns_404(client, mock_dispatch):
    r = client.post(
        "/v1/calls",
        json={
            "elder_id": str(uuid.uuid4()),
            "idempotency_key": "z",
            "dynamic_vars": {},
        },
    )
    assert r.status_code == 404


def test_get_call_returns_status(client, mock_dispatch):
    elder_id = _create_elder(client)
    created = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "g1", "dynamic_vars": {}},
    )
    call_id = created.json()["id"]
    r = client.get(f"/v1/calls/{call_id}")
    assert r.status_code == 200
    assert r.json()["id"] == call_id


def test_get_unknown_call_returns_404(client):
    r = client.get(f"/v1/calls/{uuid.uuid4()}")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -v
```

Expected: FAIL — route not registered.

- [ ] **Step 3: Write the router**

Create `apps/api/src/usan_api/routers/calls.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch
from usan_api.db.models import CallDirection, CallStatus
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/calls", tags=["calls"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CallResponse)
async def enqueue_call(
    body: CreateCallRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    elder = await elders_repo.get_elder(db, body.elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="elder not found")

    existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
    if existing is not None:
        if (
            existing.elder_id != body.elder_id
            or existing.dynamic_vars != body.dynamic_vars
        ):
            raise HTTPException(
                status_code=409,
                detail="idempotency_key reused with a different payload",
            )
        response.status_code = status.HTTP_200_OK
        return CallResponse.from_model(existing)

    if await dnc_repo.is_blocked(db, elder.phone_e164):
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
            idempotency_key=body.idempotency_key,
            dynamic_vars=body.dynamic_vars,
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).info("Outbound call blocked by DNC")
        response.status_code = status.HTTP_200_OK
        return CallResponse.from_model(call)

    room = f"usan-outbound-{uuid.uuid4()}"
    call = await calls_repo.create_call(
        db,
        elder_id=elder.id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        idempotency_key=body.idempotency_key,
        livekit_room=room,
        dynamic_vars=body.dynamic_vars,
    )
    await db.commit()

    try:
        await livekit_dispatch.dispatch_outbound_call(
            call, elder=elder, settings=settings
        )
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(
            db, call.id, CallStatus.FAILED, error={"reason": str(exc)}
        )
        await db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        await calls_repo.set_status(
            db, call.id, CallStatus.FAILED, error={"reason": "dispatch_error"}
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Outbound dispatch failed")
        raise HTTPException(
            status_code=502, detail="failed to dispatch outbound call"
        ) from exc

    await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched")
    return CallResponse.from_model(call)


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return CallResponse.from_model(call)
```

- [ ] **Step 4: Register the router**

In `apps/api/src/usan_api/main.py`, add the import:

```python
from usan_api.routers import calls
```

and inside `create_app`, before `return app`:

```python
    app.include_router(calls.router)
```

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -v
```

Expected: 7 PASS.

- [ ] **Step 6: Run the full API suite**

```bash
uv run pytest -v
```

Expected: all tests PASS (settings, health, dnc, elders, calls, livekit_dispatch).

- [ ] **Step 7: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/routers/calls.py apps/api/src/usan_api/main.py apps/api/tests/test_calls.py
git commit -m "feat(api): add /v1/calls enqueue (idempotent, DNC-gated) and get endpoints"
```

---

## Task 13: App lifespan (engine disposal) and final `main.py` review

**Files:**
- Modify: `apps/api/src/usan_api/main.py`

- [ ] **Step 1: Add a lifespan that disposes the engine on shutdown**

Replace the entire contents of `apps/api/src/usan_api/main.py` with the consolidated version (this folds in the router registrations from Tasks 8, 9, 12 and adds the lifespan):

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from usan_api.db.session import dispose_engine
from usan_api.logging_config import configure_logging
from usan_api.routers import calls, dnc, elders
from usan_api.settings import get_settings


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="USAN Voice Engine API", version="0.1.0", lifespan=lifespan
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(calls.router)

    return app
```

- [ ] **Step 2: Run the full API suite + lint + types**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Expected: tests PASS, ruff clean, mypy clean. Fix any issues (common: unused imports, missing type annotations) before continuing.

- [ ] **Step 3: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/main.py
git commit -m "feat(api): add lifespan engine disposal and consolidate routers"
```

---

## Task 14: API Dockerfile — run migrations on startup

**Files:**
- Create: `apps/api/docker-entrypoint.sh`
- Modify: `apps/api/Dockerfile`

- [ ] **Step 1: Write the entrypoint script**

Create `apps/api/docker-entrypoint.sh`:

```sh
#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting API server..."
exec uvicorn usan_api.main:create_app --factory --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: Update the Dockerfile**

Replace the entire contents of `apps/api/Dockerfile` with:

```dockerfile
# syntax=docker/dockerfile:1.7-labs
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for cache-friendliness
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root user
RUN chmod +x docker-entrypoint.sh && \
    groupadd -g 1001 appuser && \
    useradd  -u 1001 -g 1001 -s /sbin/nologin -d /app appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
```

- [ ] **Step 3: Build the image**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
DOCKER_BUILDKIT=1 docker build -t usan-api:local -f apps/api/Dockerfile apps/api
```

Expected: builds successfully.

- [ ] **Step 4: Commit**

```bash
git add apps/api/docker-entrypoint.sh apps/api/Dockerfile
git commit -m "feat(api): run alembic migrations on container startup"
```

---

## Task 15: Agent — named dispatch, metadata parsing, wait-for-answer

**Files:**
- Modify: `services/agent/src/usan_agent/settings.py`
- Modify: `services/agent/src/usan_agent/worker.py`
- Create: `services/agent/tests/test_worker.py`

- [ ] **Step 1: Write the failing test**

Create `services/agent/tests/test_worker.py`:

```python
from usan_agent.worker import CallMetadata, parse_metadata


def test_parse_metadata_outbound():
    raw = '{"call_id": "abc", "direction": "outbound", "dynamic_vars": {"name": "Ada"}}'
    md = parse_metadata(raw)
    assert md == CallMetadata(
        call_id="abc", direction="outbound", dynamic_vars={"name": "Ada"}
    )


def test_parse_metadata_none_is_inbound():
    md = parse_metadata(None)
    assert md.call_id is None
    assert md.direction == "inbound"
    assert md.dynamic_vars == {}


def test_parse_metadata_empty_string_is_inbound():
    md = parse_metadata("")
    assert md.direction == "inbound"
    assert md.call_id is None


def test_parse_metadata_invalid_json_is_inbound():
    md = parse_metadata("not json")
    assert md.direction == "inbound"
    assert md.call_id is None
    assert md.dynamic_vars == {}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_worker.py -v
```

Expected: FAIL — `ImportError: cannot import name 'CallMetadata'`.

- [ ] **Step 3: Add `agent_name` to agent settings**

In `services/agent/src/usan_agent/settings.py`, add this field inside the `Settings` class (after `default_cartesia_voice_id`):

```python
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
```

- [ ] **Step 4: Rewrite `worker.py`**

Replace the entire contents of `services/agent/src/usan_agent/worker.py` with:

```python
"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

import json
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.settings import get_settings


@dataclass(frozen=True)
class CallMetadata:
    """Per-call context passed by the API via dispatch metadata.

    Inbound dispatch-rule jobs carry no metadata, so absence means inbound.
    """

    call_id: str | None
    direction: str
    dynamic_vars: dict[str, Any] = field(default_factory=dict)


def parse_metadata(raw: str | None) -> CallMetadata:
    if not raw:
        return CallMetadata(call_id=None, direction="inbound", dynamic_vars={})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse job metadata as JSON; treating as inbound")
        return CallMetadata(call_id=None, direction="inbound", dynamic_vars={})
    return CallMetadata(
        call_id=data.get("call_id"),
        direction=data.get("direction", "inbound"),
        dynamic_vars=data.get("dynamic_vars") or {},
    )


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    meta = parse_metadata(ctx.job.metadata)
    log = logger.bind(room=ctx.room.name, call_id=meta.call_id, direction=meta.direction)
    log.info("Job assigned, connecting to room")

    await ctx.connect()
    log.info("Connected to room")

    session = build_session(settings)
    agent = build_agent()

    await session.start(agent=agent, room=ctx.room)
    log.info("Session started; waiting for participant")

    # Inbound: the caller is already present and this returns immediately.
    # Outbound: blocks until the callee answers and the SIP participant joins.
    await ctx.wait_for_participant()
    log.info("Participant present; greeting")

    await greet(session)
    log.info("Greeting spoken")


def main() -> None:
    # Configure logging first so a missing/invalid-env failure in get_settings()
    # is emitted as a structured log line, not a raw traceback.
    configure_logging()
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting USAN agent worker (agent_name={name})", name=settings.agent_name)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=settings.agent_name,
        )
    )


if __name__ == "__main__":
    main()
```

> **Note:** Setting `agent_name` makes this worker handle **explicit** dispatch only. Outbound dispatch is explicit (Task 11). Inbound now requires the dispatch rule to name this agent (Task 16 updates `livekit-sip-dispatch-rule.json`).

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest -v
```

Expected: all PASS (existing pipeline/settings tests + new worker tests).

- [ ] **Step 6: Lint + types**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Expected: clean. Fix any issues before continuing.

- [ ] **Step 7: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/src/usan_agent/settings.py services/agent/src/usan_agent/worker.py services/agent/tests/test_worker.py
git commit -m "feat(agent): named dispatch, job-metadata parsing, wait-for-participant"
```

---

## Task 16: Infra — outbound trunk, named dispatch rule, env, compose

**Files:**
- Create: `infra/livekit-sip-outbound-trunk.json`
- Modify: `infra/livekit-sip-dispatch-rule.json`
- Modify: `infra/.env.example`
- Modify: `infra/docker-compose.yml`
- Modify: `infra/README.md`

- [ ] **Step 1: Write `infra/livekit-sip-outbound-trunk.json`**

Create `infra/livekit-sip-outbound-trunk.json`:

```json
{
  "trunk": {
    "name": "usan-telnyx-outbound",
    "address": "sip.telnyx.com",
    "numbers": ["${TELNYX_CALLER_ID}"],
    "auth_username": "${TELNYX_SIP_USERNAME}",
    "auth_password": "${TELNYX_SIP_PASSWORD}"
  }
}
```

- [ ] **Step 2: Name the agent in the inbound dispatch rule**

In `infra/livekit-sip-dispatch-rule.json`, change the `agent_name` from `""` to `"usan-agent"`:

```json
{
  "dispatch_rule": {
    "name": "usan-inbound-default",
    "rule": {
      "dispatchRuleIndividual": {
        "roomPrefix": "usan-inbound-"
      }
    },
    "room_config": {
      "agents": [
        {
          "agent_name": "usan-agent"
        }
      ]
    }
  }
}
```

- [ ] **Step 3: Extend `infra/.env.example`**

Append to `infra/.env.example`:

```bash

# === Agent ===
AGENT_NAME=usan-agent

# === Outbound calling ===
# SIP outbound trunk ID returned by `livekit-cli sip create-trunk` (see infra/README.md).
# Looks like ST_xxxxxxxx.
LIVEKIT_SIP_OUTBOUND_TRUNK_ID=
# E.164 number Telnyx presents as caller ID on outbound calls (your purchased DID).
TELNYX_CALLER_ID=
```

- [ ] **Step 4: Wire new env into compose**

In `infra/docker-compose.yml`, in the `api` service `environment:` block, add:

```yaml
      LIVEKIT_SIP_OUTBOUND_TRUNK_ID: ${LIVEKIT_SIP_OUTBOUND_TRUNK_ID}
      TELNYX_CALLER_ID: ${TELNYX_CALLER_ID}
      AGENT_NAME: ${AGENT_NAME}
```

and in the `agent` service `environment:` block, add:

```yaml
      AGENT_NAME: ${AGENT_NAME}
```

- [ ] **Step 5: Document outbound setup in `infra/README.md`**

Append to `infra/README.md`:

````markdown
## Outbound calling (Plan 2a)

Outbound calls need a LiveKit **SIP outbound trunk** pointing at Telnyx, plus a
caller-ID number. The agent worker is now **named** (`AGENT_NAME=usan-agent`), so
both inbound (dispatch rule) and outbound (explicit dispatch) reference that name.

### 1. Create the outbound trunk

With the stack running and `infra/.env` populated (Telnyx SIP credentials + `TELNYX_CALLER_ID`):

```bash
set -a; . infra/.env; set +a
envsubst < infra/livekit-sip-outbound-trunk.json > /tmp/outbound.json

livekit-cli sip create-trunk \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --file /tmp/outbound.json
```

Copy the returned trunk ID (`ST_...`) into `infra/.env` as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`,
then recreate the `api` container so it picks up the new env:

```bash
make up   # or: docker compose --env-file infra/.env -f infra/docker-compose.yml up -d api
```

### 2. (Re)apply the inbound dispatch rule with the agent name

```bash
envsubst < infra/livekit-sip-dispatch-rule.json > /tmp/rule.json
livekit-cli sip create-dispatch \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --file /tmp/rule.json
```

### 3. Place an outbound call

```bash
# Add an elder
curl -s -X POST http://localhost:8000/v1/elders \
  -H 'content-type: application/json' \
  -d '{"name":"Test Elder","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'

# Enqueue a call (use the returned elder id)
curl -s -X POST http://localhost:8000/v1/calls \
  -H 'content-type: application/json' \
  -d '{"elder_id":"<ELDER_ID>","idempotency_key":"smoke-1","dynamic_vars":{}}'

# Poll status
curl -s http://localhost:8000/v1/calls/<CALL_ID>
```

Your phone should ring; on answer you hear the greeting. Watch the agent:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f agent
```

### Outbound smoke test result

Document the outcome here: network setup (local public IP / VM), what you heard,
and any latency observations. If you don't yet have a public IP for Telnyx to
route media to, note that outbound is deferred to the Plan 4 deploy task.
````

- [ ] **Step 6: Validate compose + JSON syntax**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
python -c "import json; json.load(open('infra/livekit-sip-outbound-trunk.json')); json.load(open('infra/livekit-sip-dispatch-rule.json')); print('json ok')"
docker compose --env-file infra/.env -f infra/docker-compose.yml config >/dev/null && echo "compose ok"
```

Expected: `json ok` and `compose ok`. (If `infra/.env` is missing required vars, `cp infra/.env.example infra/.env` and fill it first.)

- [ ] **Step 7: Commit**

```bash
git add infra/livekit-sip-outbound-trunk.json infra/livekit-sip-dispatch-rule.json infra/.env.example infra/docker-compose.yml infra/README.md
git commit -m "feat(infra): add outbound SIP trunk, named dispatch rule, outbound env"
```

---

## Task 17: Full-stack bring-up and migration-on-boot verification

- [ ] **Step 1: Update `infra/.env` with the new keys**

Ensure `infra/.env` includes `AGENT_NAME=usan-agent`, `TELNYX_CALLER_ID=...`, and (after Task 16 step 1) `LIVEKIT_SIP_OUTBOUND_TRUNK_ID=ST_...`. For a config-only smoke (no real call), `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`/`TELNYX_CALLER_ID` may stay empty — `POST /v1/calls` will then return `503` with a clear "outbound calling not configured" message, which is expected.

- [ ] **Step 2: Rebuild and bring up the stack**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
make base   # if the agent base image needs rebuilding
make up
docker compose --env-file infra/.env -f infra/docker-compose.yml ps
```

Expected: postgres, redis, livekit, livekit-sip, api, agent all running/healthy.

- [ ] **Step 3: Confirm migrations ran on the API container**

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs api | grep -i "migrat\|upgrade"
docker exec usan-postgres psql -U usan -d usan -c "\dt"
```

Expected: log shows "Running database migrations" then upgrade to `0001`; `\dt` lists `elders`, `dnc_list`, `calls`, plus alembic's `alembic_version`.

- [ ] **Step 4: Exercise the endpoints**

```bash
curl -sf http://localhost:8000/health
ELDER=$(curl -s -X POST http://localhost:8000/v1/elders -H 'content-type: application/json' \
  -d '{"name":"Smoke","phone_e164":"+15550009999","timezone":"UTC"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "elder: $ELDER"
curl -s -X POST http://localhost:8000/v1/calls -H 'content-type: application/json' \
  -d "{\"elder_id\":\"$ELDER\",\"idempotency_key\":\"smoke-1\",\"dynamic_vars\":{}}"
```

Expected: `{"status":"ok"}`; elder created; the call POST returns either a `dialing` call (if outbound configured) or `503 outbound calling not configured` (if trunk/caller-id empty) — both are acceptable for this step.

- [ ] **Step 5: Confirm the agent registered under its name**

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs agent | grep -i "agent_name\|registered\|usan-agent" | tail -10
```

Expected: log shows the worker starting with `agent_name=usan-agent` and registering with LiveKit.

---

## Task 18: Verification, live outbound smoke, and push

- [ ] **Step 1: Run both test suites + lint + types**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd ../../services/agent
uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: all green in both packages.

- [ ] **Step 2: Run pre-commit on all files**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
pre-commit run --all-files
```

Expected: all hooks pass. If any auto-fix, stage and commit:

```bash
git add -u && git commit -m "chore: pre-commit auto-fixes"
```

- [ ] **Step 3: Live outbound smoke (mandatory if you have a public IP)**

With `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` and `TELNYX_CALLER_ID` set and Telnyx routable, place a call to your own phone via `POST /v1/calls` (see `infra/README.md` Outbound calling §3). You should ring within a few seconds, hear the greeting on answer, and `GET /v1/calls/{id}` should show `dialing`. Document the result under `infra/README.md` "Outbound smoke test result". If no public IP is available, explicitly note the deferral there.

- [ ] **Step 4: Open a PR**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git checkout -b feat/plan-2a-outbound-foundation
git push -u origin feat/plan-2a-outbound-foundation
gh pr create --title "feat: Plan 2a — outbound call foundation" --body "$(cat <<'EOF'
## Summary
- Add async SQLAlchemy + Alembic data layer (elders, dnc_list, calls).
- Add elder, DNC, and call (idempotent, DNC-gated) REST endpoints.
- Add LiveKit outbound dispatch (agent dispatch + SIP participant).
- Agent worker: named dispatch, job-metadata parsing, wait-for-participant before greeting.
- Infra: outbound SIP trunk config, named inbound dispatch rule, migrations on container boot.

## Test plan
- [ ] `apps/api`: pytest (testcontainers Postgres), ruff, mypy green
- [ ] `services/agent`: pytest, ruff, mypy green
- [ ] Stack boots; API runs migrations on startup; `\dt` shows the three tables
- [ ] Elder/DNC/call endpoints exercised via curl
- [ ] Live outbound call to a real phone (or documented deferral)
EOF
)"
```

Expected: PR created. CI (`lint.yml`, `test.yml`) green.

> **Note (CI):** integration tests use `testcontainers`, which needs Docker. GitHub Actions `ubuntu-latest` runners provide a Docker daemon, so no workflow change is required. Confirm `test.yml` is green on the PR; if the runner can't reach Docker, switch `pytest-api` to a `services: postgres` container with a `TEST_DATABASE_URL` env var instead.

---

## Plan 2a done criteria

You can claim Plan 2a done when ALL of these are true:

1. `uv run pytest -v` passes in both `apps/api` and `services/agent`; `ruff` and `mypy` are clean in both.
2. `alembic upgrade head` creates `elders`, `dnc_list`, `calls` (+ enums) on a fresh database.
3. The stack boots with `make up`; the `api` container runs migrations on startup; `/health` returns `{"status":"ok"}`.
4. `POST /v1/elders`, `PUT /v1/elders/{id}`, `POST /v1/dnc`, `DELETE /v1/dnc/{phone}` work end-to-end.
5. `POST /v1/calls` is idempotent (200 replay / 409 conflict), DNC-gated (`dnc_blocked`), and on success dispatches the agent + SIP outbound and returns `dialing`; `GET /v1/calls/{id}` returns status.
6. The agent registers under `agent_name=usan-agent`; inbound still works via the named dispatch rule.
7. A real outbound call is answered with the greeting (or the deferral is documented in `infra/README.md`).
8. CI (`lint.yml`, `test.yml`) is green on the pushed branch.

## What's NOT in Plan 2a (deferred to Plan 2b)

- Voicemail detection (Telnyx AMD + first-3s transcript regex; cancel LLM/TTS, leave scripted message, `voicemail_left`).
- Retry orchestrator (APScheduler): re-dispatch `no_answer` / `voicemail_left` / `busy` / `failed` per §5.3 policy.
- TCPA quiet-hour gating (no calls 9pm–9am in the elder's local time zone).
- Call lifecycle status transitions driven by LiveKit/Telnyx callbacks (`ringing`, `in_progress`, `no_answer`, `completed`) — Plan 2a leaves a dispatched call at `dialing`.
