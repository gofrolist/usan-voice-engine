# USAN Voice Engine — Plan 2b-1: Call Lifecycle & Outcome Classification

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive an outbound call through its full lifecycle. After dispatch, the call no longer sits at `dialing` forever: the LiveKit SIP dial result classifies the pre-answer outcome (`in_progress` / `busy` / `no_answer` / `failed`) and a LiveKit room webhook marks the post-answer terminal state (`completed`). The dial moves off the HTTP request path into a tracked background task; failed dials tear the room down so the agent doesn't hang.

**Architecture:** Plan 2a dialed the SIP participant synchronously inside `enqueue_call` with `wait_until_answered=False` and left the call at `dialing`. Plan 2b-1 splits dispatch: `enqueue_call` does the (fast) agent dispatch and schedules a **background dial task** that calls `create_sip_participant(wait_until_answered=True, ringing_timeout=…)`. On success → `in_progress` + `answered_at` + `sip_call_id`; on a SIP failure → classify the SIP status code (486→`busy`, 408/480/487→`no_answer`, 603→`no_answer`, else→`failed`) and `delete_room`. A signature-verified `POST /webhooks/livekit` receiver maps `room_finished` → `completed` (only when the call was `in_progress`), recording `ended_at` and `duration_seconds`.

**Tech Stack:** FastAPI, SQLAlchemy 2.x async, `livekit-api` (`LiveKitAPI`, `CreateSIPParticipantRequest`, `WebhookReceiver`, `TokenVerifier`, `DeleteRoomRequest`, `google.protobuf.Duration`), asyncio background tasks, pytest + testcontainers, PyJWT (test-only, for minting webhook tokens).

**Reference spec:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` (§5.2 state machine, §8 error handling, §10 webhook signatures).
**Builds on:** the merged Plan 2a (`apps/api` calls/dnc/elders + `livekit_dispatch.py` + the agent worker).

---

## Research findings baked into this plan (read before starting)

These were verified against the installed SDKs (`livekit-api==1.1.0`, `livekit-protocol==1.1.9`) and live docs. They are *why* this plan deviates from a literal reading of the spec:

1. **Telnyx AMD / Telnyx call webhooks do NOT apply to our topology.** We dial out through Telnyx as a plain SIP trunk, so Telnyx never controls the call leg and never emits `call.machine.detection.ended` for our calls. Pre-answer outcome (busy / no-answer / failed) is therefore taken **in-band from the LiveKit SIP dial result**, not from Telnyx. (Voicemail detection is agent-side and lives in **Plan 2b-2**, not here.)
2. **An unanswered outbound call produces NO LiveKit `participant_joined`/`participant_left` webhook** (no SIP participant ever joins the room). So busy/no-answer/failed can only be learned from the synchronous `create_sip_participant` failure when `wait_until_answered=True` — that is the core reason this plan moves dialing to a blocking background task.
3. **`WebhookReceiver(TokenVerifier(api_key, api_secret)).receive(body: str, auth_token: str) -> WebhookEvent`.** `WebhookEvent.event` is a *plain string* (no enum). Canonical strings include `room_started`, `room_finished`, `participant_joined`, `participant_left`. The leave event is `participant_left` (underscore), not `participant_disconnected`. `event.room.name` carries our `livekit_room`.
4. **`ringing_timeout` / `max_call_duration` are `google.protobuf.Duration`** — build with `Duration(seconds=N)`, never a bare int.
5. **The exact `TwirpError` attribute that carries the SIP status code is version-volatile** — the SDK stub doesn't document it. This plan parses it defensively and instructs you to confirm the real shape against a live busy/no-answer in staging (Task 2's `sip_code_from_exception`).
6. **A background task must build its own `AsyncSession`** via `get_session_factory()` — the `get_db` dependency is request-scoped and unusable off-request.

---

## File structure produced by this plan

```
apps/api/
├── src/usan_api/
│   ├── settings.py                 (modify: outbound ringing/max-duration timeouts)
│   ├── background.py               (create: tracked asyncio task runner + drain)
│   ├── dialer.py                   (create: schedule_dial seam — keeps enqueue testable)
│   ├── sip_status.py               (create: pure SIP-code → CallStatus classifier)
│   ├── livekit_dispatch.py         (modify: split dispatch_agent + dial_and_classify + delete_room)
│   ├── livekit_webhooks.py         (create: verify_livekit_webhook wrapper)
│   ├── repositories/calls.py       (modify: mark_answered / mark_dial_failure / mark_completed_if_in_progress)
│   ├── routers/calls.py            (modify: enqueue does agent dispatch + schedules background dial)
│   ├── routers/webhooks.py         (create: POST /webhooks/livekit)
│   └── main.py                     (modify: register webhooks router; drain tasks in lifespan)
├── migrations/versions/
│   └── 0002_add_sip_call_id_index.py   (create)
└── tests/
    ├── test_sip_status.py          (create)
    ├── test_livekit_dispatch.py    (modify: dial_and_classify cases)
    ├── test_calls_lifecycle.py     (create: repo transitions + enqueue scheduling)
    └── test_webhooks.py            (create: livekit webhook receiver)

infra/
├── livekit.yaml                    (modify: webhook config → api)
├── docker-compose.yml              (modify: api LIVEKIT_WEBHOOK note / no new env required)
└── README.md                       (modify: lifecycle + webhook setup)
```

**Boundary discipline:** Plan 2b-1 touches **only `apps/api` and `infra`**. No `services/agent` changes — failed dials tear the room down (`delete_room`), which disconnects the waiting agent, so the agent needs no code change here. (The agent-side voicemail detector + a belt-and-suspenders answer timeout are Plan 2b-2.)

---

## Task 1: Outbound dial timeout settings

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Modify: `apps/api/tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_settings.py`:

```python
def test_outbound_dial_timeouts_have_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.delenv("OUTBOUND_RINGING_TIMEOUT_S", raising=False)
    monkeypatch.delenv("OUTBOUND_MAX_CALL_DURATION_S", raising=False)

    s = Settings()

    assert s.outbound_ringing_timeout_s == 45
    assert s.outbound_max_call_duration_s == 1800


def test_outbound_dial_timeouts_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("OUTBOUND_RINGING_TIMEOUT_S", "30")

    s = Settings()

    assert s.outbound_ringing_timeout_s == 30
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_settings.py -k outbound_dial -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'outbound_ringing_timeout_s'`.

- [ ] **Step 3: Add the fields**

In `apps/api/src/usan_api/settings.py`, inside the `Settings` class (after `agent_name`), add:

```python
    outbound_ringing_timeout_s: int = Field(
        default=45, ge=5, le=120, alias="OUTBOUND_RINGING_TIMEOUT_S"
    )
    outbound_max_call_duration_s: int = Field(
        default=1800, ge=60, le=7200, alias="OUTBOUND_MAX_CALL_DURATION_S"
    )
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_settings.py -k outbound_dial -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings.py
git commit -m "feat(api): add outbound ringing + max-call-duration settings"
```

---

## Task 2: SIP status-code classifier (pure functions)

**Files:**
- Create: `apps/api/src/usan_api/sip_status.py`
- Create: `apps/api/tests/test_sip_status.py`

This maps the SIP failure surfaced by LiveKit into a `CallStatus`. It is pure and has no LiveKit imports, so it is trivially testable; the volatile "extract the SIP code from the exception" lives here behind one function you confirm against real hardware.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_sip_status.py`:

```python
import asyncio

import pytest

from usan_api.db.base import CallStatus
from usan_api.sip_status import classify_dial_exception, sip_code_from_exception


class _FakeTwirp(Exception):
    def __init__(self, message, metadata=None):
        super().__init__(message)
        self.metadata = metadata or {}


def test_sip_code_from_metadata():
    exc = _FakeTwirp("dial failed", metadata={"sip_status_code": "486"})
    assert sip_code_from_exception(exc) == 486


def test_sip_code_from_message_fallback():
    exc = _FakeTwirp("upstream returned 480 Temporarily Unavailable")
    assert sip_code_from_exception(exc) == 480


def test_sip_code_none_when_absent():
    assert sip_code_from_exception(Exception("opaque error")) is None


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_reason"),
    [
        (486, CallStatus.BUSY, "sip_busy"),
        (408, CallStatus.NO_ANSWER, "sip_no_answer"),
        (480, CallStatus.NO_ANSWER, "sip_no_answer"),
        (487, CallStatus.NO_ANSWER, "sip_no_answer"),
        (603, CallStatus.NO_ANSWER, "sip_declined"),
        (404, CallStatus.FAILED, "sip_error"),
        (503, CallStatus.FAILED, "sip_error"),
    ],
)
def test_classify_by_sip_code(code, expected_status, expected_reason):
    exc = _FakeTwirp("x", metadata={"sip_status_code": str(code)})
    status, reason, error = classify_dial_exception(exc)
    assert status is expected_status
    assert reason == expected_reason
    assert error == {"sip_code": code}


def test_classify_timeout_is_no_answer():
    status, reason, error = classify_dial_exception(asyncio.TimeoutError())
    assert status is CallStatus.NO_ANSWER
    assert reason == "ring_timeout"


def test_classify_unknown_is_failed():
    status, reason, error = classify_dial_exception(Exception("opaque"))
    assert status is CallStatus.FAILED
    assert reason == "dial_error"
    assert error == {"reason": "Exception"}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_sip_status.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.sip_status'`.

- [ ] **Step 3: Write `sip_status.py`**

Create `apps/api/src/usan_api/sip_status.py`:

```python
"""Classify a LiveKit SIP outbound-dial failure into a CallStatus.

LiveKit raises an error from create_sip_participant(wait_until_answered=True) when
the callee is busy / does not answer / rejects. The error carries the upstream SIP
status code, but the exact attribute is version-volatile, so sip_code_from_exception
parses defensively (metadata keys first, then the message). Verify the real shape
against a live busy/no-answer before relying on the metadata path.
"""

import asyncio
import re
from typing import Any

from usan_api.db.base import CallStatus

_CODE_RE = re.compile(r"\b([4-6]\d\d)\b")
_META_KEYS = ("sip_status_code", "sip_status", "sipStatusCode", "status_code")


def sip_code_from_exception(exc: BaseException) -> int | None:
    """Best-effort extraction of the upstream SIP status code (e.g. 486)."""
    meta = getattr(exc, "metadata", None)
    if isinstance(meta, dict):
        for key in _META_KEYS:
            value = meta.get(key)
            if value is not None:
                try:
                    return int(str(value)[:3])
                except ValueError:
                    pass
    match = _CODE_RE.search(str(exc))
    return int(match.group(1)) if match else None


def classify_dial_exception(
    exc: BaseException,
) -> tuple[CallStatus, str, dict[str, Any]]:
    """Map a dial failure to (status, end_reason, error_dict)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return CallStatus.NO_ANSWER, "ring_timeout", {"reason": "timeout"}

    code = sip_code_from_exception(exc)
    if code == 486:
        return CallStatus.BUSY, "sip_busy", {"sip_code": code}
    if code in (408, 480, 487):
        return CallStatus.NO_ANSWER, "sip_no_answer", {"sip_code": code}
    if code == 603:
        return CallStatus.NO_ANSWER, "sip_declined", {"sip_code": code}
    if code is not None:
        return CallStatus.FAILED, "sip_error", {"sip_code": code}

    return CallStatus.FAILED, "dial_error", {"reason": type(exc).__name__}
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_sip_status.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/sip_status.py apps/api/tests/test_sip_status.py
git commit -m "feat(api): add SIP status-code -> CallStatus classifier"
```

---

## Task 3: Call repository lifecycle transitions

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Create: `apps/api/tests/test_calls_lifecycle.py`

Add the timestamped transitions the dialer and webhook need. `mark_completed_if_in_progress` is keyed on `livekit_room` (the webhook only knows the room name) and is a no-op unless the call is currently `in_progress` — so it never overrides a `busy`/`no_answer`/`failed`/`voicemail_left` row.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_calls_lifecycle.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


@pytest.fixture
def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_call(factory, *, status, room, answered=False):
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="A", phone_e164="+15551112222", timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=room,
        )
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_mark_answered_sets_in_progress(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r1")
    async with session_factory() as db:
        call = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_1")
        await db.commit()
    assert call.status is CallStatus.IN_PROGRESS
    assert call.answered_at is not None
    assert call.sip_call_id == "SCL_1"


@pytest.mark.asyncio
async def test_mark_dial_failure_sets_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r2")
    async with session_factory() as db:
        call = await calls_repo.mark_dial_failure(
            db, call_id, CallStatus.BUSY, end_reason="sip_busy", error={"sip_code": 486}
        )
        await db.commit()
    assert call.status is CallStatus.BUSY
    assert call.ended_at is not None
    assert call.end_reason == "sip_busy"
    assert call.error == {"sip_code": 486}


@pytest.mark.asyncio
async def test_mark_completed_only_when_in_progress(session_factory):
    # in_progress -> completed, with duration computed
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="r3")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_3")
        await db.commit()
    async with session_factory() as db:
        call = await calls_repo.mark_completed_if_in_progress(db, "r3")
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.COMPLETED
    assert call.ended_at is not None
    assert call.duration_seconds is not None and call.duration_seconds >= 0


@pytest.mark.asyncio
async def test_mark_completed_noop_when_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="r4")
    async with session_factory() as db:
        result = await calls_repo.mark_completed_if_in_progress(db, "r4")
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER  # unchanged


@pytest.mark.asyncio
async def test_mark_completed_unknown_room_is_none(session_factory):
    async with session_factory() as db:
        assert await calls_repo.mark_completed_if_in_progress(db, "nope") is None
```

> **Note:** the `async_database_url` fixture already exists in `conftest.py` from Plan 2a; it points at the testcontainers Postgres with migrations applied.

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls_lifecycle.py -v
```

Expected: FAIL — `AttributeError: module 'usan_api.repositories.calls' has no attribute 'mark_answered'`.

- [ ] **Step 3: Add the repository functions**

In `apps/api/src/usan_api/repositories/calls.py`, add the import and a UTC helper at the top (keep existing imports), and the three functions at the end of the file:

```python
from datetime import UTC, datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)
```

```python
async def mark_answered(
    db: AsyncSession, call_id: uuid.UUID, *, sip_call_id: str | None
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = CallStatus.IN_PROGRESS
    call.answered_at = _utcnow()
    if sip_call_id:
        call.sip_call_id = sip_call_id
    await db.flush()
    await db.refresh(call)
    return call


async def mark_dial_failure(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    end_reason: str,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call


async def mark_completed_if_in_progress(
    db: AsyncSession, livekit_room: str
) -> Call | None:
    result = await db.execute(select(Call).where(Call.livekit_room == livekit_room))
    call = result.scalar_one_or_none()
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = "hangup"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call
```

> **Note:** `select`, `AsyncSession`, `Call`, `CallStatus`, `Any`, and `uuid` are already imported in `repositories/calls.py` from Plan 2a. Only `datetime`/`UTC` and `_utcnow` are new.

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls_lifecycle.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_calls_lifecycle.py
git commit -m "feat(api): add call lifecycle transitions (answered/dial-failure/completed)"
```

---

## Task 4: Split the dispatcher — agent dispatch + background dial-and-classify

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py`
- Modify: `apps/api/tests/test_livekit_dispatch.py`

`dispatch_outbound_call` (Plan 2a) did agent dispatch + SIP participant in one call. Split it: `dispatch_agent` (fast, synchronous, validates config) and `dial_and_classify` (background, blocking, `wait_until_answered=True`, writes the outcome). Keep `build_livekit_api` and `OutboundDispatchError`.

- [ ] **Step 1: Write the failing test**

Replace the contents of `apps/api/tests/test_livekit_dispatch.py` with:

```python
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
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
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.fixture
def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(factory, status=CallStatus.DIALING, room="usan-outbound-x"):
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Ada", phone_e164="+15551234567", timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=room,
        )
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_dispatch_agent_requires_outbound_config():
    call = Call(id=uuid.uuid4(), direction=CallDirection.OUTBOUND, livekit_room="r", dynamic_vars={})
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    with pytest.raises(livekit_dispatch.OutboundDispatchError):
        await livekit_dispatch.dispatch_agent(call, settings=settings)


@pytest.mark.asyncio
async def test_dispatch_agent_creates_dispatch(monkeypatch):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    call = Call(
        id=uuid.uuid4(), direction=CallDirection.OUTBOUND, livekit_room="usan-outbound-y", dynamic_vars={}
    )
    await livekit_dispatch.dispatch_agent(call, settings=_settings())
    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    req = fake.agent_dispatch.create_dispatch.await_args.args[0]
    assert req.agent_name == "usan-agent"
    assert req.room == "usan-outbound-y"
    assert str(call.id) in req.metadata


@pytest.mark.asyncio
async def test_dial_success_marks_in_progress(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.return_value = MagicMock(sip_call_id="SCL_OK")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id = await _seed(session_factory, room="usan-outbound-ok")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    fake.sip.create_sip_participant.assert_awaited_once()
    sip_req = fake.sip.create_sip_participant.await_args.args[0]
    assert sip_req.wait_until_answered is True
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS
    assert call.sip_call_id == "SCL_OK"
    fake.room.delete_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_dial_busy_marks_busy_and_deletes_room(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.side_effect = _twirp_busy()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id = await _seed(session_factory, room="usan-outbound-busy")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.BUSY
    assert call.end_reason == "sip_busy"
    fake.room.delete_room.assert_awaited_once()


def _twirp_busy() -> Exception:
    exc = Exception("SIP 486 Busy Here")
    exc.metadata = {"sip_status_code": "486"}
    return exc
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_livekit_dispatch.py -v
```

Expected: FAIL — `AttributeError: module 'usan_api.livekit_dispatch' has no attribute 'dispatch_agent'`.

- [ ] **Step 3: Rewrite `livekit_dispatch.py`**

Replace the contents of `apps/api/src/usan_api/livekit_dispatch.py` with:

```python
import json
import uuid

from google.protobuf.duration_pb2 import Duration
from livekit import api
from loguru import logger

from usan_api.db.models import Call, Elder
from usan_api.db.session import get_session_factory
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings
from usan_api.sip_status import classify_dial_exception


class OutboundDispatchError(Exception):
    """Raised when an outbound call cannot be dispatched (misconfig)."""


def build_livekit_api(settings: Settings) -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_http_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def _outbound_metadata(call: Call) -> str:
    return json.dumps(
        {
            "call_id": str(call.id),
            "direction": "outbound",
            "dynamic_vars": call.dynamic_vars,
        }
    )


async def dispatch_agent(call: Call, *, settings: Settings) -> None:
    """Dispatch the named agent worker into the call's room (fast, synchronous)."""
    if not settings.livekit_sip_outbound_trunk_id or not settings.telnyx_caller_id:
        raise OutboundDispatchError(
            "outbound calling not configured: set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID and TELNYX_CALLER_ID"
        )
    if not call.livekit_room:
        raise OutboundDispatchError("call has no livekit_room assigned")

    async with build_livekit_api(settings) as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=call.livekit_room,
                metadata=_outbound_metadata(call),
            )
        )
    logger.bind(call_id=str(call.id), room=call.livekit_room).info("Agent dispatched")


async def _create_sip_participant(call: Call, elder: Elder, settings: Settings) -> object:
    async with build_livekit_api(settings) as lkapi:
        return await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                sip_call_to=elder.phone_e164,
                sip_number=settings.telnyx_caller_id,
                room_name=call.livekit_room,
                participant_identity="callee",
                participant_name=elder.name,
                wait_until_answered=True,
                play_ringtone=True,
                ringing_timeout=Duration(seconds=settings.outbound_ringing_timeout_s),
                max_call_duration=Duration(seconds=settings.outbound_max_call_duration_s),
            )
        )


async def _delete_room(room: str, settings: Settings) -> None:
    try:
        async with build_livekit_api(settings) as lkapi:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room))
    except Exception:  # best-effort cleanup; never mask the original dial outcome
        logger.bind(room=room).warning("delete_room failed during dial cleanup")


async def dial_and_classify(call_id: uuid.UUID, settings: Settings) -> None:
    """Background task: dial the callee, classify the outcome, write it, clean up."""
    factory = get_session_factory()
    async with factory() as db:
        call = await calls_repo.get_call(db, call_id)
        if call is None or call.elder_id is None or not call.livekit_room:
            logger.bind(call_id=str(call_id)).warning("dial_and_classify: call not dialable")
            return
        elder = await elders_repo.get_elder(db, call.elder_id)
        if elder is None:
            return
        room = call.livekit_room

    log = logger.bind(call_id=str(call_id), room=room)
    try:
        info = await _create_sip_participant(call, elder, settings)
    except Exception as exc:  # busy / no-answer / reject / transport
        status, end_reason, error = classify_dial_exception(exc)
        async with factory() as db:
            await calls_repo.mark_dial_failure(
                db, call_id, status, end_reason=end_reason, error=error
            )
            await db.commit()
        await _delete_room(room, settings)
        log.info("Outbound dial failed: {status} ({reason})", status=status.value, reason=end_reason)
        return

    sip_call_id = getattr(info, "sip_call_id", None)
    async with factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id=sip_call_id)
        await db.commit()
    log.info("Outbound call answered; in_progress")
```

> **Note for the engineer:** the `except Exception` catch is intentional — `create_sip_participant(wait_until_answered=True)` raises a `TwirpError` on busy/no-answer/reject, and possibly an `asyncio.TimeoutError` on ring timeout. `classify_dial_exception` (Task 2) handles all of them. **Verify in staging** that the real exception's SIP code is read by `sip_code_from_exception` — if `metadata` uses a different key, add it to `_META_KEYS` in `sip_status.py`.

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_livekit_dispatch.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/livekit_dispatch.py apps/api/tests/test_livekit_dispatch.py
git commit -m "feat(api): split dispatch into agent dispatch + background dial-and-classify"
```

---

## Task 5: Background task runner + dial scheduler seam

**Files:**
- Create: `apps/api/src/usan_api/background.py`
- Create: `apps/api/src/usan_api/dialer.py`
- Create: `apps/api/tests/test_background.py`

`background.py` tracks fire-and-forget tasks so they aren't garbage-collected mid-flight and can be drained on shutdown. `dialer.schedule_dial` is the seam the calls router uses, so the router stays unit-testable (tests monkeypatch `schedule_dial` instead of spawning real dials).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_background.py`:

```python
import asyncio

import pytest

from usan_api import background


@pytest.mark.asyncio
async def test_spawn_tracks_and_drains():
    ran = []

    async def work():
        await asyncio.sleep(0)
        ran.append(True)

    background.spawn(work())
    assert len(background.active_tasks()) >= 1
    await background.drain(timeout=2)
    assert ran == [True]
    assert background.active_tasks() == set()


@pytest.mark.asyncio
async def test_drain_with_no_tasks_is_noop():
    await background.drain(timeout=1)
    assert background.active_tasks() == set()
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_background.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.background'`.

- [ ] **Step 3: Write `background.py` and `dialer.py`**

Create `apps/api/src/usan_api/background.py`:

```python
"""Tracked fire-and-forget asyncio tasks.

asyncio holds only a weak reference to bare create_task() results, so a task can
be garbage-collected before it finishes. Keep a strong reference in a set and
drain on shutdown.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def active_tasks() -> set[asyncio.Task[Any]]:
    return set(_tasks)


async def drain(timeout: float = 30.0) -> None:
    pending = active_tasks()
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)
```

Create `apps/api/src/usan_api/dialer.py`:

```python
"""Seam between the calls router and the background SIP dial.

Kept as a one-function module so the router can be unit-tested by monkeypatching
schedule_dial without spawning a real LiveKit dial.
"""

import uuid

from usan_api import background, livekit_dispatch
from usan_api.settings import Settings


def schedule_dial(call_id: uuid.UUID, settings: Settings) -> None:
    background.spawn(livekit_dispatch.dial_and_classify(call_id, settings))
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_background.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/background.py apps/api/src/usan_api/dialer.py apps/api/tests/test_background.py
git commit -m "feat(api): add tracked background task runner + dial scheduler seam"
```

---

## Task 6: Rewire enqueue to agent-dispatch + schedule background dial

**Files:**
- Modify: `apps/api/src/usan_api/routers/calls.py`
- Modify: `apps/api/tests/test_calls.py`

`_create_and_dispatch` now: create `queued` row → commit → `dispatch_agent` (sync; 503 on misconfig, 502 on agent-dispatch error) → set `dialing` → commit → `schedule_dial`. The blocking SIP dial happens in the background; the HTTP response still returns `202` with status `dialing`.

- [ ] **Step 1: Update the calls tests**

In `apps/api/tests/test_calls.py`, the existing `mock_dispatch` fixture patches `dispatch_outbound_call`, which no longer exists. Replace that fixture and the dispatch assertions. Change the fixture to:

```python
@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    scheduled: list = []
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)

    def _schedule(call_id, settings):
        scheduled.append(call_id)

    from usan_api import dialer

    monkeypatch.setattr(dialer, "schedule_dial", _schedule)
    agent.scheduled = scheduled
    return agent
```

Then update the assertions in the existing tests:
- In `test_enqueue_call_dispatches_and_returns_202`: replace `mock_dispatch.assert_awaited_once()` with:
  ```python
  mock_dispatch.assert_awaited_once()  # agent dispatched
  assert len(mock_dispatch.scheduled) == 1  # background dial scheduled
  ```
- In `test_enqueue_call_idempotent_replay_returns_200`: replace `mock_dispatch.assert_awaited_once()` with `assert len(mock_dispatch.scheduled) == 1`.
- In `test_enqueue_call_dnc_blocked`: replace `mock_dispatch.assert_not_awaited()` with `assert mock_dispatch.scheduled == []`.

Update the two dispatch-error tests (`test_enqueue_call_dispatch_config_error_returns_503`, `test_enqueue_call_unexpected_dispatch_error_returns_502`): they currently patch `dispatch_outbound_call`; patch `dispatch_agent` instead:

```python
def test_enqueue_call_dispatch_config_error_returns_503(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise livekit_dispatch.OutboundDispatchError("not configured: set LIVEKIT_SIP_OUTBOUND_TRUNK_ID")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
    )
    assert r.status_code == 503
    assert "LIVEKIT_SIP_OUTBOUND_TRUNK_ID" not in r.json()["detail"]
    replay = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"


def test_enqueue_call_unexpected_dispatch_error_returns_502(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err502", "dynamic_vars": {}},
    )
    assert r.status_code == 502
```

Add one new test asserting the enqueued call is left at `dialing` (the background dial moves it later):

```python
def test_enqueue_call_status_is_dialing(client, mock_dispatch):
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dl", "dynamic_vars": {}},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "dialing"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -v
```

Expected: FAIL — the router still calls `livekit_dispatch.dispatch_outbound_call` (now gone) → errors/AttributeError.

- [ ] **Step 3: Update `_create_and_dispatch` in `routers/calls.py`**

In `apps/api/src/usan_api/routers/calls.py`, add the dialer import near the top:

```python
from usan_api import dialer
```

and replace the body of `_create_and_dispatch` (the dispatch + transition section, currently calling `dispatch_outbound_call`) with:

```python
async def _create_and_dispatch(
    db: AsyncSession,
    body: CreateCallRequest,
    elder: Elder,
    settings: Settings,
    response: Response,
) -> CallResponse:
    """Persist a queued call, dispatch the agent, schedule the background dial."""
    room = f"usan-outbound-{uuid.uuid4()}"
    try:
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
    except IntegrityError as exc:
        await db.rollback()
        existing = await calls_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is None:
            raise HTTPException(status_code=409, detail="idempotency_key conflict") from exc
        return _idempotent_replay(existing, body, response)

    try:
        await livekit_dispatch.dispatch_agent(call, settings=settings)
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(db, call.id, CallStatus.FAILED, error={"reason": str(exc)})
        await db.commit()
        raise HTTPException(status_code=503, detail="outbound calling is not available") from exc
    except Exception as exc:
        await calls_repo.set_status(
            db,
            call.id,
            CallStatus.FAILED,
            error={"reason": "dispatch_error", "exc_type": type(exc).__name__},
        )
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Agent dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    dialing = await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    dialer.schedule_dial(call.id, settings)
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched; dialing")
    return CallResponse.from_model(dialing or call)
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/routers/calls.py apps/api/tests/test_calls.py
git commit -m "feat(api): enqueue does agent dispatch + schedules background SIP dial"
```

---

## Task 7: LiveKit webhook receiver (`POST /webhooks/livekit`)

**Files:**
- Create: `apps/api/src/usan_api/livekit_webhooks.py`
- Create: `apps/api/src/usan_api/routers/webhooks.py`
- Modify: `apps/api/src/usan_api/main.py`
- Create: `apps/api/tests/test_webhooks.py`

The receiver verifies the LiveKit-signed auth token (a JWT whose `sha256` claim must match the body hash) and maps `room_finished` → `completed`. The handler is idempotent and tolerant of unknown event types (returns 200 quickly).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_webhooks.py`:

```python
import base64
import hashlib
import time

import jwt
import pytest

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    claims = {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest}
    return jwt.encode(claims, secret, algorithm="HS256")


def _room_finished(room: str) -> str:
    return '{"event":"room_finished","room":{"name":"%s"},"id":"ev1","createdAt":1}' % room


async def _make_in_progress_call(async_database_url, room: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    factory = async_sessionmaker(
        create_async_engine(async_database_url, poolclass=NullPool), expire_on_commit=False
    )
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="A", phone_e164="+15551112222", timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            livekit_room=room,
        )
        await calls_repo.mark_answered(db, call.id, sip_call_id="SCL")
        await db.commit()
        return call.id


def test_livekit_webhook_room_finished_completes_call(client, async_database_url):
    import asyncio

    room = "usan-outbound-wh1"
    call_id = asyncio.run(_make_in_progress_call(async_database_url, room))
    body = _room_finished(room)
    token = _sign(body, "key", "a" * 32)
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": token, "Content-Type": "application/webhook+json"},
    )
    assert r.status_code == 200
    follow = client.get(f"/v1/calls/{call_id}")
    assert follow.json()["status"] == "completed"


def test_livekit_webhook_bad_signature_rejected(client):
    body = _room_finished("usan-outbound-x")
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": "not-a-valid-token"},
    )
    assert r.status_code == 401


def test_livekit_webhook_unknown_event_ignored(client):
    body = '{"event":"track_published","room":{"name":"r"},"id":"e","createdAt":1}'
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
```

> **Note:** the `client` fixture sets `LIVEKIT_API_KEY="key"` / `LIVEKIT_API_SECRET="a"*32` (Plan 2a conftest), which is why the test signs with those values. If `WebhookReceiver` rejects the hand-minted token, inspect `apps/api/.venv/.../livekit/api/access_token.py` `Claims` parsing and add any required claim (e.g. `jti`) to `_sign`.

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_webhooks.py -v
```

Expected: FAIL — `404` (route not registered).

- [ ] **Step 3: Write the verify wrapper and the router**

Create `apps/api/src/usan_api/livekit_webhooks.py`:

```python
from livekit import api

from usan_api.settings import Settings


def verify_livekit_webhook(body: str, auth_token: str, settings: Settings) -> api.WebhookEvent:
    """Verify a LiveKit webhook and return the event. Raises on invalid signature."""
    receiver = api.WebhookReceiver(
        api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    return receiver.receive(body, auth_token)
```

Create `apps/api/src/usan_api/routers/webhooks.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_webhooks
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook event strings that signal a room (and thus the call) has ended.
_ROOM_END_EVENTS = frozenset({"room_finished"})


@router.post("/livekit", status_code=status.HTTP_200_OK)
async def livekit_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    body = (await request.body()).decode("utf-8")
    auth = request.headers.get("Authorization", "")
    try:
        event = livekit_webhooks.verify_livekit_webhook(body, auth, settings)
    except Exception as exc:  # invalid signature / hash mismatch / malformed
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc

    if event.event in _ROOM_END_EVENTS and event.room and event.room.name:
        call = await calls_repo.mark_completed_if_in_progress(db, event.room.name)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), room=event.room.name).info(
                "Call completed via room_finished webhook"
            )
    return {"ok": True}
```

- [ ] **Step 4: Register the router**

In `apps/api/src/usan_api/main.py`, add the import:

```python
from usan_api.routers import calls, dnc, elders, webhooks
```

and inside `create_app`, before `return app`:

```python
    app.include_router(webhooks.router)
```

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_webhooks.py -v
```

Expected: all PASS. (If the signature test fails because of the minted token, see the note in Step 1 and adjust `_sign`.)

- [ ] **Step 6: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/livekit_webhooks.py apps/api/src/usan_api/routers/webhooks.py apps/api/src/usan_api/main.py apps/api/tests/test_webhooks.py
git commit -m "feat(api): add signature-verified LiveKit webhook receiver (room_finished -> completed)"
```

---

## Task 8: Drain background tasks on shutdown

**Files:**
- Modify: `apps/api/src/usan_api/main.py`

- [ ] **Step 1: Update the lifespan to drain tasks before disposing the engine**

In `apps/api/src/usan_api/main.py`, add the import:

```python
from usan_api import background
```

and change the `lifespan` body so it drains in-flight dials before closing the DB:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await background.drain(timeout=30.0)
    await dispose_engine()
```

- [ ] **Step 2: Verify the app still constructs and the suite passes**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_health.py tests/test_calls.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/main.py
git commit -m "feat(api): drain in-flight dial tasks on shutdown"
```

---

## Task 9: Migration — index on `sip_call_id`

**Files:**
- Create: `apps/api/migrations/versions/0002_add_sip_call_id_index.py`

`sip_call_id` is now populated (Task 4). Index it (partial, NULLs excluded) for future webhook lookups by SIP call id.

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0002_add_sip_call_id_index.py`:

```python
"""add partial index on calls.sip_call_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-30

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_calls_sip_call_id ON calls(sip_call_id) "
        "WHERE sip_call_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_sip_call_id")
```

- [ ] **Step 2: Apply against the running Postgres and verify**

The Plan 2a stack should be up (`make up`). Apply:

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic upgrade head
docker exec usan-postgres psql -U usan -d usan -c "\di idx_calls_sip_call_id"
```

Expected: `Running upgrade 0001 -> 0002`; the index is listed. (If the local DB creds differ, the testcontainers suite already applies `0002` via `alembic upgrade head` in conftest — that is the authoritative verification.)

- [ ] **Step 3: Verify downgrade then re-upgrade**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic downgrade 0001 && \
DATABASE_URL=postgresql://usan:change-me-locally@127.0.0.1:5432/usan \
  LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=$(python -c "print('a'*32)") \
  LIVEKIT_URL=ws://livekit:7880 \
  uv run alembic upgrade head
```

Expected: clean down to `0001` then up to `0002`.

- [ ] **Step 4: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/migrations/versions/0002_add_sip_call_id_index.py
git commit -m "feat(api): add partial index on calls.sip_call_id"
```

---

## Task 10: Infra — point LiveKit webhooks at the API

**Files:**
- Modify: `infra/livekit.yaml`
- Modify: `infra/README.md`

LiveKit must POST room lifecycle events to the API. The `webhook` block signs events with one of the configured API keys; the API verifies with the same key/secret.

- [ ] **Step 1: Add the webhook block to `infra/livekit.yaml`**

In `infra/livekit.yaml`, add (top level, sibling to `keys:`):

```yaml
webhook:
  api_key: ${LIVEKIT_API_KEY}
  urls:
    - http://api:8000/webhooks/livekit
```

> **Note:** `api` is the compose service name; the LiveKit container reaches the API over the compose network. `webhook.api_key` must be one of the keys under `keys:` so LiveKit knows which secret to sign with.

- [ ] **Step 2: Document the lifecycle in `infra/README.md`**

Append to `infra/README.md`:

````markdown
## Call lifecycle (Plan 2b-1)

After dispatch, an outbound call now transitions through real states instead of
sitting at `dialing`:

- `dialing` → background SIP dial (`wait_until_answered=true`, `ringing_timeout`):
  - answered → `in_progress` (sets `answered_at`, `sip_call_id`)
  - SIP 486 → `busy`; SIP 408/480/487 → `no_answer`; SIP 603 → `no_answer`; other → `failed` (the room is torn down so the waiting agent exits)
- `in_progress` → LiveKit `room_finished` webhook → `completed` (sets `ended_at`, `duration_seconds`)

LiveKit posts room events to `http://api:8000/webhooks/livekit`, signed with
`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` and verified by the API. Confirm wiring:

```bash
# place a call, then watch it advance
curl -s -X POST http://localhost:8000/v1/calls -H 'content-type: application/json' \
  -d "{\"elder_id\":\"<ELDER_ID>\",\"idempotency_key\":\"lc-1\",\"dynamic_vars\":{}}"
# poll a few times — status moves dialing -> in_progress/busy/no_answer -> completed
curl -s http://localhost:8000/v1/calls/<CALL_ID>
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api | grep -i webhook
```

> Retry of `no_answer`/`busy`/`failed`/`voicemail_left` calls and TCPA quiet hours
> are Plan 2b-3; agent-side voicemail detection is Plan 2b-2. In 2b-1 those terminal
> states are reached and recorded but not yet retried.
````

- [ ] **Step 3: Validate compose + YAML**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
python3 -c "import yaml; yaml.safe_load(open('infra/livekit.yaml'))" && echo "yaml ok"
docker compose --env-file infra/.env.example -f infra/docker-compose.yml config >/dev/null && echo "compose ok"
```

Expected: `yaml ok` and `compose ok`.

- [ ] **Step 4: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add infra/livekit.yaml infra/README.md
git commit -m "feat(infra): point LiveKit room webhooks at the API; document call lifecycle"
```

---

## Task 11: Full verification, stack bring-up, and PR

- [ ] **Step 1: Full API suite + lint + types**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: all green. Fix any issue before continuing.

- [ ] **Step 2: Agent suite unchanged (sanity)**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest -q && uv run ruff check . && uv run mypy
```

Expected: green (Plan 2b-1 made no agent changes).

- [ ] **Step 3: Pre-commit on all files**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
pre-commit run --all-files
```

Expected: clean. Stage + commit any auto-fixes.

- [ ] **Step 4: Stack bring-up + migration-on-boot**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
make up
docker compose --env-file infra/.env -f infra/docker-compose.yml logs api | grep -i "upgrade\|0002"
docker exec usan-postgres psql -U usan -d usan -c "\di idx_calls_sip_call_id"
```

Expected: API runs migrations to `0002` on boot; the index exists.

- [ ] **Step 5: Live lifecycle smoke (mandatory if outbound is configured)**

With `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`/`TELNYX_CALLER_ID` set and Telnyx routable, place a call to your own phone, then to a number that is off / declines, and confirm the call rows reach `in_progress`→`completed` and `no_answer`/`busy` respectively (`GET /v1/calls/{id}`). **Confirm the real busy/no-answer SIP code is parsed** by `sip_code_from_exception` — if the resulting status is `failed` (the unknown-code fallback) instead of `busy`/`no_answer`, inspect the raised exception's attributes in the API logs and extend `_META_KEYS` in `sip_status.py`. Record the result under `infra/README.md` "Outbound smoke test result". If no public IP, document the deferral.

- [ ] **Step 6: Open the PR**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git checkout -b feat/plan-2b-1-call-lifecycle
git push -u origin feat/plan-2b-1-call-lifecycle
gh pr create --title "feat: Plan 2b-1 — call lifecycle & outcome classification" --body "$(cat <<'EOF'
## Summary
- Move the SIP dial off the request path into a tracked background task (wait_until_answered=true + ringing_timeout).
- Classify pre-answer outcome from the LiveKit SIP dial result: 486->busy, 408/480/487/603->no_answer, else->failed; tear the room down on failure.
- On answer: in_progress + answered_at + sip_call_id.
- Signature-verified POST /webhooks/livekit: room_finished -> completed (+ ended_at, duration_seconds), idempotent and in_progress-gated.
- Drain in-flight dials on shutdown; partial index on calls.sip_call_id; LiveKit webhook wiring in infra.

Telnyx AMD is intentionally NOT used (our LiveKit-SIP-trunk topology can't invoke it); voicemail detection is Plan 2b-2 and retry/quiet-hours are Plan 2b-3.

## Test plan
- [ ] apps/api: pytest (testcontainers), ruff, mypy green
- [ ] services/agent: unchanged, green
- [ ] Stack boots, migrates to 0002, webhook wired
- [ ] Live: call reaches in_progress->completed; unanswered reaches no_answer/busy (SIP code parsed) — or documented deferral
EOF
)"
```

Expected: PR created; CI green.

> **Note (TwirpError):** the one genuinely unverified detail is the exact attribute on the LiveKit dial exception that carries the SIP status code. `sip_code_from_exception` parses `metadata` keys then the message; if a real busy/no-answer lands as `failed`, capture the exception shape from the logs (Step 5) and extend `_META_KEYS`. This is the only build-time confirmation required.

---

## Plan 2b-1 done criteria

1. `uv run pytest`/`ruff`/`mypy` green in `apps/api`; agent package unchanged and green.
2. `alembic upgrade head` reaches `0002`; `idx_calls_sip_call_id` exists.
3. `POST /v1/calls` returns `202`/`dialing`; the background dial then advances the call to `in_progress`/`busy`/`no_answer`/`failed`.
4. A `room_finished` LiveKit webhook (signature-verified) moves an `in_progress` call to `completed` with `ended_at`+`duration_seconds`; non-`in_progress` rows are untouched.
5. A failed dial tears down the room (agent does not hang) and records the terminal status + `end_reason`.
6. CI green on the pushed branch; live lifecycle smoke validated or its deferral documented.

## What's NOT in Plan 2b-1 (next sub-plans)

- **Plan 2b-2:** agent-side voicemail detector (first-3s STT regex → `session.interrupt(force=True)` → scripted `session.say` → `ctx.delete_room`), the agent→API JWT auth + `POST /v1/calls/{id}/status` callback endpoint, and a belt-and-suspenders agent-side answer timeout.
- **Plan 2b-3:** the retry orchestrator (DB-driven `FOR UPDATE SKIP LOCKED` poller in the FastAPI lifespan), the §5.3 retry policy as a pure function, `parent_call_id`/`attempt` retry chaining, TCPA quiet hours via `zoneinfo` (+ `tzdata` dependency), DNC re-check at retry, and extending `idx_calls_status_scheduled` to cover `busy`/`failed`.
- **Deferred entirely:** Telnyx AMD (only viable if outbound origination moves to the Telnyx Voice API) and a generic Telnyx webhook receiver.

> **Note on the `ringing` state:** the blocking `wait_until_answered=True` dial goes from `dialing` straight to `in_progress` (answered) or a terminal failure — it does not observe SIP 180 Ringing, so `CallStatus.RINGING` stays unused in 2b-1. That's intentional: `ringing` is a transient state with no behavioral consequence (retries key off terminal states). It can be surfaced later from the `sip.callStatus` participant attribute if a UI ever needs it.
