# Plan 3a — In-Call Tool API Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the JWT-authenticated `/v1/tools/*` endpoints the agent calls during a live call — `log_wellness`, `log_medication`, `get_today_meds`, `end_call` — backed by new `wellness_logs` / `medication_logs` / `transcripts` tables.

**Architecture:** Migration 0004 creates the three spec tables (§5.1); each gets an ORM model + repository. A new `/v1/tools` router reuses the existing `require_service_token` JWT dependency and the `call_id`-scoping pattern: every tool takes `call_id` in its body, the handler asserts `claims["call_id"] == call_id`, loads the call, and **derives `elder_id` from the call** (never trusts a request param). `get_today_meds` reads the elder's schedule from `elder.meta["medication_schedule"]` (the v1 storage decision — no dedicated table). `end_call` completes an in-progress call with a caller-supplied `end_reason` (gated/idempotent, races the `room_finished` webhook safely). The `transcripts` table is created here but its flush wiring is deferred to a later plan (3c).

**Tech Stack:** FastAPI, SQLAlchemy 2.x async (asyncpg), Alembic, Pydantic v2, PyJWT (HS256), pytest + testcontainers (`pgvector/pgvector:pg18`).

---

## Context for the implementer

Work in `apps/api` (Python 3.14, `uv`). Run everything from `apps/api/`:

```bash
cd apps/api && uv sync
uv run pytest -v
uv run ruff check . && uv run ruff format .
uv run mypy
```

Conventions (reviewers enforce):
- **ruff** `select = ["E","F","I","B","UP","ASYNC","S","PT","RET","SIM"]`, line-length 100, `S` ignored under `tests/**`. No `try/except/pass` (S110) — log or use `contextlib.suppress`; no `async def` param literally named `timeout` (ASYNC109). For a `pytest.raises(...)` block, keep its body to a single statement (PT012) — put setup before/outside the `with`.
- **mypy** `--strict` on `src` only.
- Commit format `type(scope): description`, scope `api`. **No `Co-Authored-By` trailer** (attribution disabled).
- Three-layer pattern: router (`routers/`) → repository (`repositories/`) → schema (`schemas/`). Repositories `flush`/`refresh` but never `commit`; routers `commit`.

### Existing code you will reuse (on `main`, after Plan 2b-3)

- `src/usan_api/auth.py` — `require_service_token(credentials=Depends(HTTPBearer(auto_error=False)), settings=Depends(get_settings)) -> dict[str, Any]`. 401 on missing/wrong-scheme; verifies HS256 with `settings.jwt_signing_key`, requires `exp` + `call_id` claims. **The caller must check `claims["call_id"]` matches the resource.**
- `src/usan_api/db/base.py` — `Base`, `CallStatus` (incl. `IN_PROGRESS`, `COMPLETED`), `CallDirection`.
- `src/usan_api/db/models.py` — `Elder` (has `meta: Mapped[dict] = mapped_column("metadata", JSONB, ...)`, `phone_e164`, `timezone`), `Call`, `DNCEntry`. Uses `Mapped`/`mapped_column`, `UUID(as_uuid=True)`, `JSONB`, `func.now()`, `text("...")`.
- `src/usan_api/db/session.py` — `get_db` (FastAPI dependency, AsyncSession).
- `src/usan_api/repositories/calls.py` — `get_call(db, call_id) -> Call | None`, `_utcnow()`, and `mark_completed_if_in_progress(db, livekit_room)` (the gated-completion pattern to mirror for `end_call`). Imports already include `uuid`, `datetime`/`UTC`, `select`, `AsyncSession`, `Call`, `CallStatus`, `_utcnow`.
- `src/usan_api/repositories/elders.py` — `get_elder(db, elder_id) -> Elder | None`.
- `src/usan_api/routers/elders.py` + `routers/calls.py` — the router template (esp. `report_outcome`: `claims: dict[str, Any] = Depends(require_service_token)` then `if claims.get("call_id") != str(call_id): raise HTTPException(403, ...)`).
- `src/usan_api/main.py` — `create_app()` registers routers via `app.include_router(...)`.
- `migrations/versions/0001_initial_schema.py` (raw-SQL `op.execute` style), `0003_retry_indexes.py` (`revision`/`down_revision` header style). Latest revision is **`0003`**.
- `tests/conftest.py` — session-scoped `database_url` (testcontainer + `alembic upgrade head`), `async_database_url`, and `client` (sets env incl. `JWT_SIGNING_KEY="s"*32`, overrides `get_db` with a NullPool engine; truncates `calls, dnc_list, elders` on teardown). `tests/test_calls.py` has the reusable `_create_elder`, `_service_token`, `mock_dispatch`, and `_answered_call` patterns.

### Spec tables (§5.1) implemented here

```sql
CREATE TABLE transcripts (
    id          BIGSERIAL PRIMARY KEY,
    call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_name   TEXT,
    tool_args   JSONB,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transcripts_call ON transcripts(call_id, started_at);

CREATE TABLE wellness_logs (
    id            BIGSERIAL PRIMARY KEY,
    call_id       UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    elder_id      UUID NOT NULL REFERENCES elders(id),
    mood          SMALLINT,
    pain_level    SMALLINT,
    notes         TEXT,
    raw           JSONB NOT NULL DEFAULT '{}',
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE medication_logs (
    id              BIGSERIAL PRIMARY KEY,
    call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    elder_id        UUID NOT NULL REFERENCES elders(id),
    medication_name TEXT NOT NULL,
    taken           BOOLEAN NOT NULL,
    reported_time   TIMESTAMPTZ,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Tool API surface

| Tool | Body | Auth | Effect |
|---|---|---|---|
| `POST /v1/tools/log_wellness` | `{call_id, mood?, pain_level?, notes?}` | JWT + `call_id` scope | insert `wellness_logs` (elder_id from call) |
| `POST /v1/tools/log_medication` | `{call_id, medication_name, taken, reported_time?}` | JWT + `call_id` scope | insert `medication_logs` |
| `POST /v1/tools/get_today_meds` | `{call_id}` | JWT + `call_id` scope | return `elder.meta["medication_schedule"]` |
| `POST /v1/tools/end_call` | `{call_id, reason}` | JWT + `call_id` scope | complete the in-progress call with `end_reason=reason` |

### Decisions locked (do not silently change)

1. **`call_id` in the body**, validated against the JWT `call_id` claim (matches spec §4.1). `elder_id` is always derived from the loaded call — never accepted from the request.
2. **Medication schedule lives in `elder.meta["medication_schedule"]`** as a list of `{name, dosage?, times}` — no dedicated table in v1. `get_today_meds` returns it as-is (no day/time filtering yet; that's a future enhancement).
3. **`end_call`** completes an `in_progress` call (gated → idempotent), computing `duration_seconds`, with the caller's `reason` as `end_reason`. It races the existing `room_finished` webhook safely (whichever marks `completed` first wins; the other no-ops).
4. **`transcripts` table is created now, flush wiring deferred** to Plan 3c.
5. A call whose `elder_id` is NULL (the elder was deleted — `ON DELETE SET NULL`) cannot be logged against → `409`.

---

## File Structure

**Create:**
- `migrations/versions/0004_tool_logging_tables.py` — the three tables.
- `src/usan_api/repositories/wellness.py` — `create_wellness_log`.
- `src/usan_api/repositories/medications.py` — `create_medication_log`.
- `src/usan_api/schemas/tools.py` — request/response models.
- `src/usan_api/routers/tools.py` — the `/v1/tools` router + a shared authorize helper.
- `tests/test_tools.py` — endpoint tests.

**Modify:**
- `src/usan_api/db/models.py` — add `Transcript`, `WellnessLog`, `MedicationLog` (+ `BigInteger`, `Boolean` imports).
- `src/usan_api/repositories/calls.py` — add `complete_call_if_in_progress`.
- `src/usan_api/main.py` — register the tools router.
- `tests/test_calls_lifecycle.py` — a test for `complete_call_if_in_progress`.

---

## Task 1: Models + migration 0004 (tool-logging tables)

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py`
- Create: `apps/api/migrations/versions/0004_tool_logging_tables.py`
- Test: `apps/api/tests/test_tool_models.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_tool_models.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, MedicationLog, Transcript, WellnessLog
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            livekit_room="usan-outbound-tm",
        )
        await db.commit()
        return call.id, elder.id


@pytest.mark.asyncio
async def test_wellness_log_round_trip(session_factory):
    call_id, elder_id = await _seed_call(session_factory)
    async with session_factory() as db:
        row = WellnessLog(call_id=call_id, elder_id=elder_id, mood=4, pain_level=2, notes="ok")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(WellnessLog, row_id)
    assert got is not None
    assert got.mood == 4
    assert got.pain_level == 2
    assert got.notes == "ok"
    assert got.raw == {}
    assert got.logged_at is not None


@pytest.mark.asyncio
async def test_medication_log_round_trip(session_factory):
    call_id, elder_id = await _seed_call(session_factory)
    async with session_factory() as db:
        row = MedicationLog(
            call_id=call_id, elder_id=elder_id, medication_name="Aspirin", taken=True
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(MedicationLog, row_id)
    assert got is not None
    assert got.medication_name == "Aspirin"
    assert got.taken is True
    assert got.reported_time is None


@pytest.mark.asyncio
async def test_transcript_round_trip(session_factory):
    call_id, _ = await _seed_call(session_factory)
    async with session_factory() as db:
        row = Transcript(
            call_id=call_id, role="user", content="hello", started_at=calls_repo._utcnow()
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    async with session_factory() as db:
        got = await db.get(Transcript, row_id)
    assert got is not None
    assert got.role == "user"
    assert got.content == "hello"
    assert got.tool_name is None
    assert got.created_at is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_tool_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'MedicationLog' from 'usan_api.db.models'`.

- [ ] **Step 3: Add the ORM models**

In `apps/api/src/usan_api/db/models.py`, change the first SQLAlchemy import line (line 6) from:

```python
from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, Text, text
```

to:

```python
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, SmallInteger, Text, text
```

Then append these three classes at the end of `models.py`:

```python
class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(Text)
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WellnessLog(Base):
    __tablename__ = "wellness_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id"), nullable=False
    )
    mood: Mapped[int | None] = mapped_column(SmallInteger)
    pain_level: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MedicationLog(Base):
    __tablename__ = "medication_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id"), nullable=False
    )
    medication_name: Mapped[str] = mapped_column(Text, nullable=False)
    taken: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reported_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Write the migration**

Create `apps/api/migrations/versions/0004_tool_logging_tables.py`:

```python
"""tool-logging tables: transcripts, wellness_logs, medication_logs

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-31

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcripts (
            id          BIGSERIAL PRIMARY KEY,
            call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            tool_name   TEXT,
            tool_args   JSONB,
            started_at  TIMESTAMPTZ NOT NULL,
            ended_at    TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_transcripts_call ON transcripts(call_id, started_at)")

    op.execute(
        """
        CREATE TABLE wellness_logs (
            id            BIGSERIAL PRIMARY KEY,
            call_id       UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id      UUID NOT NULL REFERENCES elders(id),
            mood          SMALLINT,
            pain_level    SMALLINT,
            notes         TEXT,
            raw           JSONB NOT NULL DEFAULT '{}',
            logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE medication_logs (
            id              BIGSERIAL PRIMARY KEY,
            call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id        UUID NOT NULL REFERENCES elders(id),
            medication_name TEXT NOT NULL,
            taken           BOOLEAN NOT NULL,
            reported_time   TIMESTAMPTZ,
            logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS medication_logs")
    op.execute("DROP TABLE IF EXISTS wellness_logs")
    op.execute("DROP TABLE IF EXISTS transcripts")
```

- [ ] **Step 5: Run the test to verify it passes**

A fresh `pytest` invocation runs `alembic upgrade head` (incl. 0004) via the session-scoped fixture.

Run: `cd apps/api && uv run pytest tests/test_tool_models.py -v`
Expected: PASS (3 cases). Confirm the chain: `uv run alembic history | head` shows `0003 -> 0004 (head)`.

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/db/models.py migrations/versions/0004_tool_logging_tables.py tests/test_tool_models.py
git commit -m "feat(api): add tool-logging tables + models (migration 0004)"
```

---

## Task 2: `log_wellness` endpoint (+ tools router + authorize helper)

**Files:**
- Create: `apps/api/src/usan_api/repositories/wellness.py`
- Create: `apps/api/src/usan_api/schemas/tools.py`
- Create: `apps/api/src/usan_api/routers/tools.py`
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_tools.py`:

```python
import time
import uuid

import jwt
import pytest

from usan_api import livekit_dispatch


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _create_elder(client, *, metadata: dict | None = None) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": metadata or {},
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"tool-{uuid.uuid4()}", "dynamic_vars": {}},
    )
    assert r.status_code == 202
    return r.json()["id"]


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def test_log_wellness_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 4, "pain_level": 2, "notes": "feeling good"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_wellness_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
    )
    assert r.status_code == 401


def test_log_wellness_token_call_id_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


def test_log_wellness_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": cid, "mood": 3},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_log_wellness_out_of_range_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 9},  # mood is 1..5
        headers=_auth(call_id),
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_tools.py -v`
Expected: FAIL — all `/v1/tools/log_wellness` requests 404 (router not registered).

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/wellness.py`:

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WellnessLog


async def create_wellness_log(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    mood: int | None,
    pain_level: int | None,
    notes: str | None,
) -> WellnessLog:
    row = WellnessLog(
        call_id=call_id,
        elder_id=elder_id,
        mood=mood,
        pain_level=pain_level,
        notes=notes,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
```

- [ ] **Step 4: Write the schemas**

Create `apps/api/src/usan_api/schemas/tools.py`:

```python
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """Base for in-call tool requests: the call this tool action belongs to.

    The handler asserts this matches the JWT `call_id` claim and derives elder_id
    from the call — elder_id is never accepted from the request.
    """

    call_id: uuid.UUID


class LogWellnessRequest(ToolCallRequest):
    mood: int | None = Field(default=None, ge=1, le=5)
    pain_level: int | None = Field(default=None, ge=0, le=10)
    notes: str | None = Field(default=None, max_length=2000)


class LoggedResponse(BaseModel):
    id: int
```

- [ ] **Step 5: Write the router (with the shared authorize helper)**

Create `apps/api/src/usan_api/routers/tools.py`:

```python
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_service_token
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.tools import LoggedResponse, LogWellnessRequest

router = APIRouter(prefix="/v1/tools", tags=["tools"])


async def _authorize_call(call_id: uuid.UUID, claims: dict[str, Any], db: AsyncSession) -> Call:
    """Verify the JWT is scoped to this call and load it (404 if unknown)."""
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return call


def _require_elder(call: Call) -> uuid.UUID:
    if call.elder_id is None:
        raise HTTPException(status_code=409, detail="call has no associated elder")
    return call.elder_id


@router.post("/log_wellness", response_model=LoggedResponse)
async def log_wellness(
    body: LogWellnessRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await wellness_repo.create_wellness_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        mood=body.mood,
        pain_level=body.pain_level,
        notes=body.notes,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged wellness")
    return LoggedResponse(id=row.id)
```

- [ ] **Step 6: Register the router**

In `apps/api/src/usan_api/main.py`, add `tools` to the routers import and registration. Change the import line:

```python
from usan_api.routers import calls, dnc, elders, webhooks
```

to:

```python
from usan_api.routers import calls, dnc, elders, tools, webhooks
```

and add, alongside the other `app.include_router(...)` calls in `create_app()`:

```python
    app.include_router(tools.router)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_tools.py -v`
Expected: PASS (5 cases).

- [ ] **Step 8: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/wellness.py src/usan_api/schemas/tools.py src/usan_api/routers/tools.py src/usan_api/main.py tests/test_tools.py
git commit -m "feat(api): add /v1/tools/log_wellness endpoint"
```

---

## Task 3: `log_medication` endpoint

**Files:**
- Create: `apps/api/src/usan_api/repositories/medications.py`
- Modify: `apps/api/src/usan_api/schemas/tools.py`
- Modify: `apps/api/src/usan_api/routers/tools.py`
- Test: `apps/api/tests/test_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_tools.py`:

```python
def test_log_medication_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_medication_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": False},
    )
    assert r.status_code == 401


def test_log_medication_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(wrong),
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_tools.py -k log_medication -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/medications.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import MedicationLog


async def create_medication_log(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    medication_name: str,
    taken: bool,
    reported_time: datetime | None,
) -> MedicationLog:
    row = MedicationLog(
        call_id=call_id,
        elder_id=elder_id,
        medication_name=medication_name,
        taken=taken,
        reported_time=reported_time,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
```

- [ ] **Step 4: Add the request schema**

In `apps/api/src/usan_api/schemas/tools.py`, append:

```python
class LogMedicationRequest(ToolCallRequest):
    medication_name: str = Field(min_length=1, max_length=200)
    taken: bool
    reported_time: datetime | None = None
```

- [ ] **Step 5: Add the endpoint**

In `apps/api/src/usan_api/routers/tools.py`, add `medications as medications_repo` and `LogMedicationRequest` to the imports:

```python
from usan_api.repositories import medications as medications_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.tools import LoggedResponse, LogMedicationRequest, LogWellnessRequest
```

and append the endpoint:

```python
@router.post("/log_medication", response_model=LoggedResponse)
async def log_medication(
    body: LogMedicationRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await medications_repo.create_medication_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        medication_name=body.medication_name,
        taken=body.taken,
        reported_time=body.reported_time,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged medication")
    return LoggedResponse(id=row.id)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_tools.py -v`
Expected: PASS (8 cases).

- [ ] **Step 7: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/medications.py src/usan_api/schemas/tools.py src/usan_api/routers/tools.py tests/test_tools.py
git commit -m "feat(api): add /v1/tools/log_medication endpoint"
```

---

## Task 4: `get_today_meds` endpoint

**Files:**
- Modify: `apps/api/src/usan_api/schemas/tools.py`
- Modify: `apps/api/src/usan_api/routers/tools.py`
- Test: `apps/api/tests/test_tools.py`

Reads the elder's medication schedule from `elder.meta["medication_schedule"]` (a list of `{name, dosage?, times}`). Returns it as-is; malformed entries are skipped.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_tools.py`:

```python
_SCHEDULE = [
    {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]},
    {"name": "Metformin", "times": ["08:00", "20:00"]},
]


def test_get_today_meds_returns_schedule(client, mock_dispatch):
    elder_id = _create_elder(client, metadata={"medication_schedule": _SCHEDULE})
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 2
    assert meds[0] == {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}
    assert meds[1] == {"name": "Metformin", "dosage": None, "times": ["08:00", "20:00"]}


def test_get_today_meds_empty_when_no_schedule(client, mock_dispatch):
    elder_id = _create_elder(client)  # no medication_schedule in meta
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["medications"] == []


def test_get_today_meds_skips_malformed_entries(client, mock_dispatch):
    bad = [{"name": "Good", "times": ["09:00"]}, {"dosage": "x"}, "nonsense"]
    elder_id = _create_elder(client, metadata={"medication_schedule": bad})
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 1
    assert meds[0]["name"] == "Good"


def test_get_today_meds_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_tools.py -k get_today_meds -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 3: Add the schemas**

In `apps/api/src/usan_api/schemas/tools.py`, append:

```python
class MedicationScheduleItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dosage: str | None = Field(default=None, max_length=200)
    times: list[str] = Field(default_factory=list)


class GetTodayMedsRequest(ToolCallRequest):
    pass


class TodayMedsResponse(BaseModel):
    medications: list[MedicationScheduleItem]
```

- [ ] **Step 4: Add the endpoint**

In `apps/api/src/usan_api/routers/tools.py`, extend the imports:

```python
from pydantic import ValidationError

from usan_api.repositories import elders as elders_repo
from usan_api.schemas.tools import (
    GetTodayMedsRequest,
    LoggedResponse,
    LogMedicationRequest,
    LogWellnessRequest,
    MedicationScheduleItem,
    TodayMedsResponse,
)
```

and append the endpoint:

```python
@router.post("/get_today_meds", response_model=TodayMedsResponse)
async def get_today_meds(
    body: GetTodayMedsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> TodayMedsResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    elder = await elders_repo.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=409, detail="call has no associated elder")
    raw = elder.meta.get("medication_schedule", [])
    items: list[MedicationScheduleItem] = []
    if isinstance(raw, list):
        for entry in raw:
            try:
                items.append(MedicationScheduleItem.model_validate(entry))
            except ValidationError:
                logger.bind(elder_id=str(elder_id)).warning("Skipping malformed medication entry")
    return TodayMedsResponse(medications=items)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_tools.py -v`
Expected: PASS (12 cases).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/schemas/tools.py src/usan_api/routers/tools.py tests/test_tools.py
git commit -m "feat(api): add /v1/tools/get_today_meds endpoint"
```

---

## Task 5: `end_call` endpoint (+ gated completion repo fn)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Modify: `apps/api/src/usan_api/schemas/tools.py`
- Modify: `apps/api/src/usan_api/routers/tools.py`
- Test: `apps/api/tests/test_calls_lifecycle.py`, `apps/api/tests/test_tools.py`

`complete_call_if_in_progress(db, call_id, *, end_reason)` mirrors `mark_completed_if_in_progress` but keys on `call_id` and takes the reason from the caller. `end_call` invokes it; whichever of this and the `room_finished` webhook marks `completed` first wins, the other no-ops.

- [ ] **Step 1: Write the failing repo test**

Append to `apps/api/tests/test_calls_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_complete_call_if_in_progress_sets_reason(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="cc1")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL")
        await db.commit()
    async with session_factory() as db:
        call = await calls_repo.complete_call_if_in_progress(
            db, call_id, end_reason="check_in_complete"
        )
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.COMPLETED
    assert call.end_reason == "check_in_complete"
    assert call.ended_at is not None
    assert call.duration_seconds is not None
    assert call.duration_seconds >= 0


@pytest.mark.asyncio
async def test_complete_call_if_in_progress_noop_when_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="cc2")
    async with session_factory() as db:
        result = await calls_repo.complete_call_if_in_progress(
            db, call_id, end_reason="check_in_complete"
        )
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER  # unchanged
```

- [ ] **Step 2: Run the repo test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_calls_lifecycle.py -k complete_call_if_in_progress -v`
Expected: FAIL — `AttributeError: ... has no attribute 'complete_call_if_in_progress'`.

- [ ] **Step 3: Add the repository function**

Append to `apps/api/src/usan_api/repositories/calls.py`:

```python
async def complete_call_if_in_progress(
    db: AsyncSession, call_id: uuid.UUID, *, end_reason: str
) -> Call | None:
    """Mark an in-progress call COMPLETED with a caller-supplied end_reason.

    Gated on IN_PROGRESS so it is idempotent and races the room_finished webhook
    safely: whichever marks the call COMPLETED first wins, the other no-ops.
    """
    call = await db.get(Call, call_id)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 4: Run the repo test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_calls_lifecycle.py -k complete_call_if_in_progress -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Write the failing endpoint tests**

Append to `apps/api/tests/test_tools.py`:

```python
def _answered_call(client, async_database_url) -> str:
    """Enqueue a call, then force it to IN_PROGRESS with a direct write."""
    import asyncio as _asyncio
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)

    async def _answer() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                await calls_repo.mark_answered(db, _uuid.UUID(call_id), sip_call_id="SCL")
                await db.commit()
        finally:
            await engine.dispose()

    _asyncio.run(_answer())
    return call_id


def test_end_call_completes_in_progress(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_end_call_idempotent_noop(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    first = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "user_ended"},
        headers=_auth(call_id),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "completed"  # unchanged; reason not overwritten


def test_end_call_mismatch_403(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "x"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403
```

- [ ] **Step 6: Run the endpoint tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_tools.py -k end_call -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 7: Add the schema + endpoint**

In `apps/api/src/usan_api/schemas/tools.py`, append:

```python
class EndCallRequest(ToolCallRequest):
    reason: str = Field(min_length=1, max_length=100)


class CallEndedResponse(BaseModel):
    status: str
```

In `apps/api/src/usan_api/routers/tools.py`, add `EndCallRequest` and `CallEndedResponse` to the `schemas.tools` import, then append the endpoint:

```python
@router.post("/end_call", response_model=CallEndedResponse)
async def end_call(
    body: EndCallRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallEndedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    updated = await calls_repo.complete_call_if_in_progress(db, call.id, end_reason=body.reason)
    await db.commit()
    logger.bind(call_id=str(call.id)).info("end_call requested: {r}", r=body.reason)
    return CallEndedResponse(status=(updated or call).status.value)
```

- [ ] **Step 8: Run all tool tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_tools.py tests/test_calls_lifecycle.py -v`
Expected: PASS (15 tool cases + the lifecycle suite).

- [ ] **Step 9: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/calls.py src/usan_api/schemas/tools.py src/usan_api/routers/tools.py tests/test_tools.py tests/test_calls_lifecycle.py
git commit -m "feat(api): add /v1/tools/end_call endpoint"
```

---

## Self-Review

**1. Spec coverage (§4.1 tool endpoints, §5.1 tables):**
- `log_wellness` / `log_medication` / `get_today_meds` / `end_call` → Tasks 2–5, each JWT-authed + `call_id`-scoped, `elder_id` derived from the call.
- `wellness_logs` / `medication_logs` / `transcripts` tables + models → Task 1 (migration 0004). `transcripts` is created but flush is deferred (Plan 3c) — explicitly noted.
- Medication-schedule source = `elder.meta["medication_schedule"]` (the locked decision); `get_today_meds` returns it, skipping malformed entries.
- `rag_search` (the fifth spec tool) is **out of scope** — it belongs to the RAG plan, not this one.

**2. Placeholder scan:** Every step contains complete, runnable code — models, migration, repos, schemas, router, and verbatim tests. No "TBD"/"add validation"/"similar to Task N". Bounds are concrete (mood 1–5, pain 0–10, names ≤200, reason ≤100).

**3. Type consistency:** `ToolCallRequest.call_id: uuid.UUID` is the shared base; `LoggedResponse.id: int` matches the `BigInteger` PK; `complete_call_if_in_progress(db, call_id, *, end_reason)` mirrors the existing `mark_completed_if_in_progress` shape; `_authorize_call` / `_require_elder` helper names are used consistently across all four endpoints; `elders_repo.get_elder`, `calls_repo.get_call`, `require_service_token` signatures match the codebase. The `main.py` router import/registration names (`tools`) are consistent.

**Notes for the implementer:** Each migration TDD cycle relies on a fresh `pytest` invocation re-running `alembic upgrade head` (the session-scoped `database_url` fixture) — run the failing step, add the migration, then re-run. Keep `pytest.raises` blocks single-statement (PT012). The `client` fixture truncates `calls/dnc_list/elders` but NOT the new log tables between tests — tests here assert on their own freshly-created rows (by returned id / by count scoped to their call), so cross-test residue is harmless; do not add global `COUNT(*)` assertions.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-31-plan-3a-in-call-tools.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, two-stage review (spec compliance then code quality) between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Note:** This is the API foundation for **Plan 3b** (the agent conversation loop that *calls* these endpoints via the `api_client.py` JWT pattern). Execute from a worktree branched off the current `main` (which has Plan 2b-3 merged).
