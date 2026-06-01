# Plan 3c — Transcript Flush Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each outbound check-in's conversation to the `transcripts` table — the agent reads its conversation history at call end and POSTs it in one batch to a new JWT-authed API endpoint.

**Architecture:** Final-flush-at-call-end (the chosen v1 strategy). The agent registers a `JobContext` shutdown callback that, after the session closes, maps `session.history.items` (user/assistant `ChatMessage`s + `FunctionCall`s) into transcript segments and POSTs them (best-effort) to `POST /v1/tools/log_transcript`. The API endpoint (mirroring the Plan 3a `/v1/tools/*` pattern: `require_service_token` + `_authorize_call` call_id scoping) bulk-inserts them into the `transcripts` table that Plan 3a already created. Flush is best-effort — a failure never errors the call; the transcript is non-load-bearing vs. the wellness/medication logs, which already persist synchronously.

**Tech Stack:** apps/api (FastAPI, SQLAlchemy async, Pydantic) + services/agent (livekit-agents 1.5.14 `JobContext.add_shutdown_callback`, `session.history`; httpx; PyJWT).

---

## Context for the implementer

This plan spans **both services** in one worktree. Sync and run each independently:

```bash
cd apps/api && uv sync && uv run pytest -v && uv run ruff check . && uv run ruff format . && uv run mypy
cd services/agent && uv sync && uv run pytest -v && uv run ruff check . && uv run ruff format . && uv run mypy
```

Conventions (both services): ruff `["E","F","I","B","UP","ASYNC","S","PT","RET","SIM"]`, line 100, `S` ignored in `tests/**`; mypy `--strict` on `src`; `asyncio_mode=auto` (agent) / pytest with testcontainers (api); commit `type(scope): description` (scope `api` or `agent`), **no `Co-Authored-By`**. `services/agent` must NOT import `apps/api` (HTTP only).

### Verified facts (installed livekit-agents 1.5.14 + Plan 3a code)

- **Shutdown hook:** `ctx.add_shutdown_callback(callback)` where `ctx` is the `JobContext`; `callback` is `async def` taking **0 or 1** arg (optional shutdown `reason: str`). It fires **after** `session.aclose()` (so `session.history` is fully populated) and **after** the room is disconnected (so do NOT make room API calls in it — an HTTP POST is fine).
- **History:** `session.history` is a `ChatContext`; `session.history.items` is an ordered `list` of items. Each item has `.type`:
  - `"message"` → `ChatMessage`: `.role` (`"user"`/`"assistant"`/`"system"`/`"developer"`), `.text_content` (`str | None`, joins text parts), `.created_at` (Unix float).
  - `"function_call"` → `FunctionCall`: `.name` (str), `.arguments` (JSON **string**), `.call_id`, `.created_at`.
  - `"function_call_output"` → `FunctionCallOutput` (we ignore these in v1).
- **Plan 3a `transcripts` table / `Transcript` ORM model** (already on `main`): `id` (BigInt PK), `call_id` (UUID FK→calls CASCADE, NOT NULL), `role` (Text **NOT NULL**), `content` (Text **NOT NULL**), `tool_name` (Text nullable), `tool_args` (JSONB nullable), `started_at` (TIMESTAMPTZ **NOT NULL**), `ended_at` (TIMESTAMPTZ nullable), `created_at` (server default `now()`).
- **Plan 3a tool pattern** (`apps/api/src/usan_api/routers/tools.py`): `router = APIRouter(prefix="/v1/tools")`; `_authorize_call(call_id, claims, db)` asserts `claims["call_id"] == str(call_id)` (403), loads the call (404), returns it; `ToolCallRequest(BaseModel)` base has `call_id: uuid.UUID`; endpoints take `claims: dict[str, Any] = Depends(require_service_token)` + `db = Depends(get_db)`, call a repo, `await db.commit()`. Repos flush, routers commit.
- **Agent `api_client.py`** (`services/agent`): `_mint_token(call_id, settings)` (HS256, `_TOKEN_TTL_S=300`); `report_voicemail_left` is the **best-effort POST** pattern (swallows all exceptions) — `flush_transcript` mirrors it.
- **Agent `worker.py`** outbound branch (after Plan 3b): builds `CheckInData(call_id, settings, job_ctx=ctx)`, `session = build_session(settings, userdata=data)`, `agent = build_check_in_agent()`, `await session.start(agent=agent, room=ctx.room)`, then the voicemail-detection window. This is where the flush is registered.

### Decisions locked

1. **Final flush at call end** via `ctx.add_shutdown_callback` reading `session.history.items` — not incremental streaming (v1 simplification of §9, documented).
2. **Capture user + assistant messages + tool calls.** Skip `system`/`developer` messages and empty-content messages; skip `FunctionCallOutput` (the agent's next message conveys the result). A `FunctionCall` becomes a `role="tool"` segment with `tool_name` + parsed `tool_args` (and `content` = the tool name, since `content` is NOT NULL).
3. **Best-effort, outbound-only.** Registered only for outbound calls with a `call_id` (same guard as the check-in agent). A flush failure logs a warning and never raises.
4. **Endpoint:** `POST /v1/tools/log_transcript` with `{call_id, segments:[...]}`, JWT-`call_id`-scoped via `_authorize_call` (no elder needed). Bulk-insert.
5. **`started_at`** for each segment = the item's `created_at` (Unix float → ISO-8601 UTC string); the API coerces it to `datetime`. `ended_at` left null in v1.

---

## File Structure

**Create:**
- `apps/api/src/usan_api/repositories/transcripts.py` — `create_transcript_segments`.
- `services/agent/src/usan_agent/transcript.py` — `history_to_segments` + `register_transcript_flush`.
- `apps/api/tests/test_transcripts_repo.py`, `services/agent/tests/test_transcript.py`.

**Modify:**
- `apps/api/src/usan_api/schemas/tools.py` — `TranscriptSegmentIn`, `LogTranscriptRequest`, `TranscriptLoggedResponse`.
- `apps/api/src/usan_api/routers/tools.py` — `log_transcript` endpoint.
- `apps/api/tests/test_tools.py` — endpoint tests.
- `services/agent/src/usan_agent/api_client.py` — `flush_transcript`.
- `services/agent/src/usan_agent/worker.py` — register the flush on outbound.
- `services/agent/tests/test_worker.py` — flush-registration test.

---

## Task 1: API — `log_transcript` endpoint

**Files:**
- Create: `apps/api/src/usan_api/repositories/transcripts.py`
- Modify: `apps/api/src/usan_api/schemas/tools.py`, `apps/api/src/usan_api/routers/tools.py`
- Test: `apps/api/tests/test_transcripts_repo.py`, `apps/api/tests/test_tools.py`

Work in `apps/api`.

- [ ] **Step 1: Write the failing repo test**

Create `apps/api/tests/test_transcripts_repo.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Transcript
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import transcripts as transcripts_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            livekit_room="usan-outbound-tr",
        )
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_create_transcript_segments_bulk_inserts(session_factory):
    call_id = await _seed_call(session_factory)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    segments = [
        {"role": "assistant", "content": "Hello!", "started_at": now},
        {"role": "user", "content": "I'm good", "started_at": now},
        {
            "role": "tool",
            "content": "log_wellness",
            "tool_name": "log_wellness",
            "tool_args": {"mood": 4},
            "started_at": now,
        },
    ]
    async with session_factory() as db:
        count = await transcripts_repo.create_transcript_segments(
            db, call_id=call_id, segments=segments
        )
        await db.commit()
    assert count == 3
    async with session_factory() as db:
        total = await db.execute(
            select(func.count()).select_from(Transcript).where(Transcript.call_id == call_id)
        )
        rows = await db.execute(
            select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.id)
        )
    assert total.scalar_one() == 3
    tool_row = [r for r in rows.scalars().all() if r.role == "tool"][0]
    assert tool_row.tool_name == "log_wellness"
    assert tool_row.tool_args == {"mood": 4}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_transcripts_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.repositories.transcripts'`.

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/transcripts.py`:

```python
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Transcript


async def create_transcript_segments(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    segments: Sequence[Any],
) -> int:
    """Bulk-insert transcript segments for a call. Returns the number inserted.

    Each segment must expose: role, content, started_at, and optionally
    tool_name, tool_args, ended_at (Pydantic models or mappings both work via
    attribute/`getattr`).
    """
    rows = [
        Transcript(
            call_id=call_id,
            role=_field(seg, "role"),
            content=_field(seg, "content"),
            tool_name=_field(seg, "tool_name"),
            tool_args=_field(seg, "tool_args"),
            started_at=_field(seg, "started_at"),
            ended_at=_field(seg, "ended_at"),
        )
        for seg in segments
    ]
    db.add_all(rows)
    await db.flush()
    return len(rows)


def _field(seg: Any, name: str) -> Any:
    return seg.get(name) if isinstance(seg, dict) else getattr(seg, name, None)
```

> `started_at` and `datetime` typing: `started_at` comes through as a `datetime` (Pydantic-coerced in the router, or a real `datetime` in the repo test). The `datetime` import is used only for clarity in the test; the repo stores whatever `_field` returns.

- [ ] **Step 4: Run the repo test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_transcripts_repo.py -v`
Expected: PASS (1 case).

- [ ] **Step 5: Write the failing endpoint tests**

Append to `apps/api/tests/test_tools.py`:

```python
def test_log_transcript_inserts_segments(client, mock_dispatch, async_database_url):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [
                {"role": "assistant", "content": "Hello!", "started_at": "2026-06-01T12:00:00Z"},
                {"role": "user", "content": "I'm good", "started_at": "2026-06-01T12:00:05Z"},
                {
                    "role": "tool",
                    "content": "log_wellness",
                    "tool_name": "log_wellness",
                    "tool_args": {"mood": 4},
                    "started_at": "2026-06-01T12:00:06Z",
                },
            ],
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_log_transcript_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={"call_id": call_id, "segments": [
            {"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}
        ]},
    )
    assert r.status_code == 401


def test_log_transcript_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={"call_id": call_id, "segments": [
            {"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}
        ]},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_log_transcript_empty_segments_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={"call_id": call_id, "segments": []},
        headers=_auth(call_id),
    )
    assert r.status_code == 422
```

- [ ] **Step 6: Run the endpoint tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_tools.py -k log_transcript -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 7: Add the schemas**

In `apps/api/src/usan_api/schemas/tools.py`, append:

```python
class TranscriptSegmentIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1)
    tool_name: str | None = Field(default=None, max_length=200)
    tool_args: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime | None = None


class LogTranscriptRequest(ToolCallRequest):
    segments: list[TranscriptSegmentIn] = Field(min_length=1, max_length=500)


class TranscriptLoggedResponse(BaseModel):
    count: int
```

> `schemas/tools.py` already imports `datetime` (Plan 3a) and `Any`/`Field`/`BaseModel`; if `Any` is not yet imported there, add `from typing import Any`.

- [ ] **Step 8: Add the endpoint**

In `apps/api/src/usan_api/routers/tools.py`, add the repo + schema imports:

```python
from usan_api.repositories import transcripts as transcripts_repo
```
and extend the `schemas.tools` import to include `LogTranscriptRequest` and `TranscriptLoggedResponse`. Then append the endpoint:

```python
@router.post("/log_transcript", response_model=TranscriptLoggedResponse)
async def log_transcript(
    body: LogTranscriptRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> TranscriptLoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    count = await transcripts_repo.create_transcript_segments(
        db, call_id=call.id, segments=body.segments
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged {n} transcript segments", n=count)
    return TranscriptLoggedResponse(count=count)
```

- [ ] **Step 9: Run all tool tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_tools.py tests/test_transcripts_repo.py -v`
Expected: PASS (existing tool tests + 4 new + 1 repo).

- [ ] **Step 10: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/transcripts.py src/usan_api/schemas/tools.py src/usan_api/routers/tools.py tests/test_transcripts_repo.py tests/test_tools.py
git commit -m "feat(api): add /v1/tools/log_transcript endpoint"
```

---

## Task 2: Agent — history mapping + flush client

**Files:**
- Create: `services/agent/src/usan_agent/transcript.py`
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_transcript.py`

Work in `services/agent`.

- [ ] **Step 1: Write the failing tests**

Create `services/agent/tests/test_transcript.py`:

```python
from types import SimpleNamespace

import httpx
import pytest

from usan_agent import api_client, transcript
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c",
        GEMINI_API_KEY="g",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="s" * 32,
    )


def _msg(role, text, ts):
    return SimpleNamespace(type="message", role=role, text_content=text, created_at=ts)


def _call(name, arguments, ts):
    return SimpleNamespace(type="function_call", name=name, arguments=arguments, created_at=ts)


def test_history_to_segments_maps_messages_and_tools():
    items = [
        _msg("system", "You are an assistant.", 100.0),  # skipped
        _msg("assistant", "Hello!", 101.0),
        _msg("user", "I'm good", 102.0),
        _call("log_wellness", '{"mood": 4}', 103.0),
        SimpleNamespace(type="function_call_output", name="log_wellness", output="ok", created_at=104.0),  # ignored
        _msg("assistant", "", 105.0),  # empty content skipped
    ]
    segs = transcript.history_to_segments(items)
    assert [s["role"] for s in segs] == ["assistant", "user", "tool"]
    assert segs[0]["content"] == "Hello!"
    assert segs[2]["tool_name"] == "log_wellness"
    assert segs[2]["tool_args"] == {"mood": 4}
    assert segs[2]["content"] == "log_wellness"
    # started_at is an ISO-8601 string
    assert all(isinstance(s["started_at"], str) and "T" in s["started_at"] for s in segs)


def test_history_to_segments_bad_tool_args_defaults_empty():
    segs = transcript.history_to_segments([_call("x", "not json", 1.0)])
    assert segs[0]["tool_args"] == {}


def test_history_to_segments_empty():
    assert transcript.history_to_segments([]) == []


class _FakeClient:
    captured: dict = {}
    status = 200

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return httpx.Response(_FakeClient.status, json={"count": len(json["segments"])},
                              request=httpx.Request("POST", url))


async def test_flush_transcript_posts(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 200
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    segs = [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00+00:00"}]
    await api_client.flush_transcript("call-1", _settings(), segs)
    assert _FakeClient.captured["url"] == "http://api:8000/v1/tools/log_transcript"
    assert _FakeClient.captured["json"] == {"call_id": "call-1", "segments": segs}
    assert _FakeClient.captured["headers"]["Authorization"].startswith("Bearer ")


async def test_flush_transcript_is_best_effort(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 500
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    # Must NOT raise even on a 500.
    await api_client.flush_transcript("call-1", _settings(), [
        {"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00+00:00"}
    ])


async def test_register_transcript_flush_registers_and_posts(monkeypatch):
    posted = {}

    async def _fake_flush(call_id, settings, segments):
        posted["call_id"] = call_id
        posted["segments"] = segments

    monkeypatch.setattr(transcript.api_client, "flush_transcript", _fake_flush)

    callbacks = []
    ctx = SimpleNamespace(add_shutdown_callback=lambda cb: callbacks.append(cb))
    session = SimpleNamespace(history=SimpleNamespace(items=[_msg("user", "hi", 1.0)]))

    transcript.register_transcript_flush(ctx, session, "call-1", _settings())
    assert len(callbacks) == 1
    await callbacks[0]()  # simulate the job shutdown firing the callback
    assert posted["call_id"] == "call-1"
    assert posted["segments"][0]["content"] == "hi"


async def test_register_transcript_flush_skips_empty(monkeypatch):
    called = {"n": 0}

    async def _fake_flush(call_id, settings, segments):
        called["n"] += 1

    monkeypatch.setattr(transcript.api_client, "flush_transcript", _fake_flush)
    callbacks = []
    ctx = SimpleNamespace(add_shutdown_callback=lambda cb: callbacks.append(cb))
    session = SimpleNamespace(history=SimpleNamespace(items=[]))  # no segments
    transcript.register_transcript_flush(ctx, session, "call-1", _settings())
    await callbacks[0]()
    assert called["n"] == 0  # nothing to flush -> no POST
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_transcript.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.transcript'` and `api_client` has no `flush_transcript`.

- [ ] **Step 3: Write `flush_transcript`**

In `services/agent/src/usan_agent/api_client.py`, append (uses the existing `Any` import from Task-1-style additions; if `Any` isn't imported, add `from typing import Any`):

```python
async def flush_transcript(
    call_id: str, settings: Settings, segments: list[dict[str, Any]]
) -> None:
    """Best-effort: POST the call's transcript segments at call end. Never raises."""
    url = f"{settings.api_base_url}/v1/tools/log_transcript"
    headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"call_id": call_id, "segments": segments}, headers=headers
            )
            response.raise_for_status()
        logger.bind(call_id=call_id).info("Flushed {n} transcript segments", n=len(segments))
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to flush transcript to API")
```

- [ ] **Step 4: Write `transcript.py`**

Create `services/agent/src/usan_agent/transcript.py`:

```python
"""Final-flush transcript persistence (design spec §9, v1).

At call end the JobContext shutdown callback reads the session's conversation
history, maps it to transcript segments (user/assistant messages + tool calls),
and POSTs them once (best-effort). Pure mapping in history_to_segments keeps it
unit-testable; the LiveKit history item shapes are duck-typed.
"""

import json
from datetime import UTC, datetime
from typing import Any

from usan_agent import api_client
from usan_agent.settings import Settings

_MESSAGE_ROLES = ("user", "assistant")


def _iso(created_at: float) -> str:
    return datetime.fromtimestamp(created_at, tz=UTC).isoformat()


def history_to_segments(items: list[Any]) -> list[dict[str, Any]]:
    """Map session.history.items to transcript-segment dicts.

    Keeps user/assistant messages with non-empty text and function calls (as
    role="tool" with parsed args); skips system/developer messages and
    function_call_output items.
    """
    segments: list[dict[str, Any]] = []
    for item in items:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            role = getattr(item, "role", None)
            content = getattr(item, "text_content", None)
            if role not in _MESSAGE_ROLES or not content:
                continue
            segments.append(
                {"role": role, "content": content, "started_at": _iso(item.created_at)}
            )
        elif item_type == "function_call":
            try:
                args = json.loads(item.arguments)
            except (ValueError, TypeError):
                args = {}
            segments.append(
                {
                    "role": "tool",
                    "content": item.name,
                    "tool_name": item.name,
                    "tool_args": args if isinstance(args, dict) else {},
                    "started_at": _iso(item.created_at),
                }
            )
    return segments


def register_transcript_flush(
    ctx: Any, session: Any, call_id: str, settings: Settings
) -> None:
    """Register a JobContext shutdown callback that flushes the transcript once."""

    async def _flush() -> None:
        segments = history_to_segments(session.history.items)
        if segments:
            await api_client.flush_transcript(call_id, settings, segments)

    ctx.add_shutdown_callback(_flush)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_transcript.py -v`
Expected: PASS (8 cases). Then `uv run pytest -v` (no regression).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/transcript.py src/usan_agent/api_client.py tests/test_transcript.py
git commit -m "feat(agent): map call history to transcript segments + flush client"
```

---

## Task 3: Agent — register the flush on outbound calls

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_worker.py`

Work in `services/agent`.

- [ ] **Step 1: Write the failing test**

Append to `services/agent/tests/test_worker.py` (reuse the existing `_settings` helper there):

```python
async def test_outbound_registers_transcript_flush(monkeypatch):
    _settings(monkeypatch)

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.on = MagicMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda: MagicMock())
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    registered = {}

    def _fake_register(ctx, session, call_id, settings):
        registered["call_id"] = call_id
        registered["ctx"] = ctx

    monkeypatch.setattr(worker, "register_transcript_flush", _fake_register)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    assert registered["call_id"] == "call-1"
    assert registered["ctx"] is ctx


async def test_inbound_does_not_register_transcript_flush(monkeypatch):
    _settings(monkeypatch)

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())

    called = {"n": 0}
    monkeypatch.setattr(
        worker, "register_transcript_flush", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)
    assert called["n"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_worker.py -k transcript_flush -v`
Expected: FAIL — `worker` has no `register_transcript_flush`.

- [ ] **Step 3: Wire it into the outbound branch**

In `services/agent/src/usan_agent/worker.py`, add the import (keep imports alphabetically ordered for ruff I001):

```python
from usan_agent.transcript import register_transcript_flush
```

Then, in `entrypoint`, inside the outbound branch — after `await session.start(agent=agent, room=ctx.room)` and the `CheckInData`/check-in-agent setup, before/around the voicemail-window logic — register the flush. The cleanest spot is immediately after the session is started in the outbound path. Locate the outbound branch (`if meta.direction == "outbound":`) and add, right after the participant-answered point where `meta.call_id` is known to be set (i.e. alongside building the `CheckInData`):

```python
    if meta.direction == "outbound" and meta.call_id:
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_check_in_agent()
        register_transcript_flush(ctx, session, meta.call_id, settings)
    else:
        session = build_session(settings)
        agent = build_agent()
    await session.start(agent=agent, room=ctx.room)
```

> This registers the shutdown-callback flush only for outbound calls with a `call_id` — the same guard as the check-in agent. The rest of `entrypoint` (the voicemail window, inbound greet) is unchanged. Registering before `session.start` is fine: the callback only reads `session.history` when the job shuts down, by which point the session has run.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: PASS. Then `uv run pytest -v` (full suite green).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/worker.py tests/test_worker.py
git commit -m "feat(agent): flush the transcript at the end of outbound calls"
```

---

## Self-Review

**1. Spec coverage (§9 transcript storage):**
- Conversation persisted to the `transcripts` table → Task 1 endpoint + repo (bulk insert into the Plan-3a table).
- Agent captures user + assistant + tool calls and flushes at call end → Task 2 (`history_to_segments`, `flush_transcript`) + Task 3 (shutdown-callback registration).
- Final-flush (not debounced streaming) is the locked v1 decision, documented; incremental streaming is a noted future enhancement.
- Recording (the other half of §9) remains a separate deploy-time plan.

**2. Placeholder scan:** Every step has complete, runnable code — repo, schemas, endpoint, mapping function, best-effort client, and worker wiring, with verbatim tests. No "TBD"/"add validation"/"similar to Task N".

**3. Type/name consistency:** `create_transcript_segments(db, *, call_id, segments)` is called by the router with the Pydantic `segments` list, and tested with dict segments — `_field` handles both. `history_to_segments` emits dicts with exactly the keys the API's `TranscriptSegmentIn` accepts (`role`, `content`, `tool_name`, `tool_args`, `started_at`); `started_at` is an ISO string that Pydantic coerces to `datetime`. `flush_transcript` / `register_transcript_flush` signatures match their call sites and tests. The endpoint path `/v1/tools/log_transcript` matches the agent's POST URL.

**Notes for the implementer:** `started_at` is NOT NULL — every segment carries one (the item's `created_at` → ISO). The shutdown callback must not call room APIs (the room is already disconnected when it fires) — it only POSTs over HTTP. Keep the flush best-effort (never raise). If the installed `add_shutdown_callback` or `session.history` accessors differ from what's described, adjust the affected line and note it (the design is unchanged).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-01-plan-3c-transcript-flush.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.

**2. Inline Execution** — execute in this session using executing-plans.

**Note:** This spans both services in one worktree (Task 1 in `apps/api`, Tasks 2–3 in `services/agent`) — branch off the current `main` (which has 3a + 3b). It completes the conversation arc's data capture; remaining roadmap items are inbound flow, recording, observability, and deployment.
