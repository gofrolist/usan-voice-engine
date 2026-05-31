# Plan 2b-3 — Retry Orchestrator & TCPA Quiet Hours Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically re-dial calls that ended in `no_answer` / `voicemail_left` / `busy` / `failed` per the §5.3 policy, gated by TCPA quiet hours, via an in-process Postgres poller.

**Architecture:** A retry is a **new `calls` row** linked to its predecessor by `parent_call_id` (`attempt = parent.attempt + 1`), preserving per-attempt transcript/recording history. Terminal-state code paths **PUSH** a scheduled `queued` child (its `scheduled_at` clamped into the elder's 09:00–21:00 local window); an in-process poller **PULLS** due children with `FOR UPDATE SKIP LOCKED`, re-checks DNC, and dispatches them. A stuck-`dialing` reaper recovers rows stranded by an ungraceful process death.

**Tech Stack:** FastAPI, SQLAlchemy 2.x async (asyncpg), Alembic, Pydantic-settings, `zoneinfo` (stdlib), loguru, pytest + testcontainers (`pgvector/pgvector:pg18`).

---

## Context for the implementer

You are working in `apps/api` (Python 3.14, `uv`). Run everything from `apps/api/`:

```bash
cd apps/api && uv sync
uv run pytest -v
uv run ruff check . && uv run ruff format .
uv run mypy
```

Conventions that the reviewers WILL enforce:

- **ruff** `select = ["E","F","I","B","UP","ASYNC","S","PT","RET","SIM"]`, line-length 100, `S` is ignored in `tests/**`. Notable gotchas this plan hits:
  - **No bare `try/except/pass`** (S110) and **no `try/except` where `contextlib.suppress` fits** (SIM105). For an *expected* exception that you genuinely ignore (the poller's interval sleep timing out), use `contextlib.suppress(...)`. For an *unexpected* exception you must **log it**, never `pass`.
  - **ASYNC109**: never *declare* a parameter named `timeout` on an `async def`. Passing `timeout=` as a keyword *argument* to `asyncio.wait_for(...)` is fine.
  - `B` (bugbear) is on; `BLE` (blind-except) is **not** selected, so `except Exception:` is allowed (and used by the existing crash guards).
- **mypy** `--strict` on `src` only (not `tests`). Every new signature must be fully typed.
- Commit format `type(scope): description`, scope `api`. Attribution trailers are disabled globally — do **not** add `Co-Authored-By`.
- loguru with lazy `{name}` placeholders and `logger.bind(...)`.
- `apps/api` must NOT import from `services/agent`.

### Existing code you will extend (already on the branch)

- `src/usan_api/db/base.py` — `CallStatus` enum: `QUEUED, DIALING, RINGING, IN_PROGRESS, COMPLETED, VOICEMAIL_LEFT, NO_ANSWER, BUSY, FAILED, DNC_BLOCKED, CANCELLED`. `CallDirection.OUTBOUND/INBOUND`.
- `src/usan_api/db/models.py` — `Call` already has `parent_call_id` (FK→`calls.id`), `attempt` (SmallInteger default 1), `scheduled_at` (TIMESTAMPTZ), `started_at`, `answered_at`, `ended_at`, `duration_seconds`, `end_reason`, `error`, `updated_at` (`onupdate=func.now()`). `Elder` has `timezone` (Text) and `phone_e164`.
- `src/usan_api/repositories/calls.py` — `_utcnow()`, `create_call`, `get_call`, `get_by_idempotency_key`, `set_status`, `mark_answered`, `mark_dial_failure`, `mark_completed_if_in_progress`, `mark_voicemail_left_if_in_progress`.
- `src/usan_api/repositories/dnc.py` — `lock_phone(db, phone_e164)` (transaction-scoped advisory lock), `is_blocked(db, phone_e164) -> bool`.
- `src/usan_api/repositories/elders.py` — `get_elder(db, elder_id)`.
- `src/usan_api/livekit_dispatch.py` — `OutboundDispatchError`, `build_livekit_api`, `dispatch_agent(call, *, settings)`, `_create_sip_participant`, `_delete_room(room, settings)`, `dial_and_classify(call_id, settings)` (guarded), `_dial_and_classify` (inner).
- `src/usan_api/dialer.py` — `schedule_dial(call_id, settings)` → `background.spawn(livekit_dispatch.dial_and_classify(...))`.
- `src/usan_api/background.py` — `spawn(coro)`, `active_tasks()`, `drain(timeout=30.0)`.
- `src/usan_api/main.py` — `create_app()`, `lifespan` (currently shutdown-only: drain + dispose_engine).
- `src/usan_api/db/session.py` — `get_session_factory()`, `dispose_engine()`.
- `src/usan_api/settings.py` — `Settings` (Pydantic), `get_settings()` (`lru_cache`), `outbound_ringing_timeout_s`.
- `src/usan_api/sip_status.py` — `classify_dial_exception(exc)`.
- `migrations/versions/0001_initial_schema.py`, `0002_add_sip_call_id_index.py`.
- `tests/conftest.py` — session-scoped `database_url` (testcontainer + `alembic upgrade head`), `async_database_url`, and `client` (note: `client` uses `yield TestClient(app)` **without** a `with` block, so the FastAPI **lifespan does not run** during normal API tests).

### §5.3 Retry policy (the single source of truth for this plan)

| Terminal status | `attempt` that just ended | Next-attempt delay |
|---|---|---|
| `no_answer` | 1 | +30 min |
| `no_answer` | 2 | +2 h |
| `no_answer` | ≥3 | **stop** |
| `voicemail_left` | 1 | +3 h |
| `voicemail_left` | ≥2 | **stop** |
| `busy` | 1 | +5 min |
| `busy` | ≥2 | **stop** |
| `failed` (transport) | 1 | +1 min |
| `failed` | ≥2 | **stop** |
| any other status | any | **stop** |

`no_answer` therefore makes **3 total attempts** (1 initial + 2 retries). All others make **2 total** (1 initial + 1 retry). **TCPA**: never schedule a retry before **09:00** or at/after **21:00** in the elder's local timezone (half-open `[09:00, 21:00)`).

### Design decisions locked by an adversarial review (do not silently change these)

1. **DB-enforced idempotency.** A partial UNIQUE index on `parent_call_id` guarantees at most one retry child per parent. `schedule_retry` inserts inside a SAVEPOINT and treats `IntegrityError` as "already scheduled → skip". This is race- and replica-safe; the SELECT-only guard from the first draft was rejected.
2. **Atomic mark + schedule.** Every terminal mark and its `schedule_retry` share **one transaction / one commit**. A crash between two commits would otherwise drop a retry silently.
3. **Stuck-`dialing` reaper.** Rows stranded in `dialing` (ungraceful death mid-dispatch) are re-queued after `RETRY_STUCK_DIALING_S`. Only retry rows (`scheduled_at IS NOT NULL`) are reaped.
4. **DNC re-checked at dispatch (PULL) time**, not at schedule time — closes the window where an elder is added to DNC between scheduling and the due time.
5. **`failed` retries cover the enqueue path too.** A transient agent-dispatch failure in `POST /v1/calls` (the 502 branch) schedules a retry. Permanent misconfig (`OutboundDispatchError` → 503, and the dial path's `not_configured`) does **not** retry.
6. **Quiet hours fail CLOSED.** An invalid elder timezone aborts scheduling (logged at ERROR) rather than falling back to UTC, which could place a 3 a.m. local call.
7. **Initial calls are NOT quiet-hours gated** and dial immediately (`scheduled_at` stays `NULL`, so the poller never claims them). The upstream scheduler owns quiet hours for the *first* call; the spec (§10) scopes orchestrator enforcement to retries. Documented as an explicit v1 decision.
8. **Per-replica pollers, no leader election.** Safe because `FOR UPDATE SKIP LOCKED` + the unique index make claiming and scheduling idempotent across replicas. `RETRY_POLLER_ENABLED` lets an operator confine the poller to one replica if desired.

---

## File Structure

**Create:**
- `src/usan_api/retry_policy.py` — pure `next_retry_delay(status, attempt) -> timedelta | None`.
- `src/usan_api/quiet_hours.py` — pure `next_allowed(dt_utc, tz_name) -> datetime` (+ `QUIET_START_HOUR`, `QUIET_END_HOUR`).
- `src/usan_api/retry_orchestrator.py` — `poll_once(...)`, `run_poller(...)`.
- `migrations/versions/0003_retry_indexes.py` — unique-child + due-retry partial indexes.
- `tests/test_retry_policy.py`, `tests/test_quiet_hours.py`, `tests/test_retry_indexes.py`, `tests/test_retry_scheduling.py`, `tests/test_retry_orchestrator.py`, `tests/test_dispatch_and_dial.py`, `tests/test_lifespan_poller.py`.

**Modify:**
- `src/usan_api/settings.py` — 4 new settings.
- `src/usan_api/repositories/calls.py` — `schedule_retry`, `claim_due_retries`, `reclaim_stuck_dialing`, `mark_failed_if_active`.
- `src/usan_api/livekit_dispatch.py` — wire `schedule_retry` into failure sites; add `dispatch_and_dial`.
- `src/usan_api/routers/calls.py` — wire `schedule_retry` into the voicemail outcome and the enqueue-502 path.
- `src/usan_api/main.py` — start/stop the poller in the lifespan.
- `tests/test_settings.py`, `tests/test_livekit_dispatch.py`, `tests/test_calls.py` — extend.
- `infra/.env.example`, `infra/docker-compose.yml`, `infra/README.md`.

---

## Task 1: Retry settings

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_settings.py`:

```python
def test_retry_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.delenv("RETRY_POLL_INTERVAL_S", raising=False)
    monkeypatch.delenv("RETRY_BATCH_SIZE", raising=False)
    monkeypatch.delenv("RETRY_STUCK_DIALING_S", raising=False)
    monkeypatch.delenv("RETRY_POLLER_ENABLED", raising=False)

    s = Settings()

    assert s.retry_poll_interval_s == 30
    assert s.retry_batch_size == 20
    assert s.retry_stuck_dialing_s == 300
    assert s.retry_poller_enabled is True


def test_retry_settings_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("RETRY_POLL_INTERVAL_S", "15")
    monkeypatch.setenv("RETRY_BATCH_SIZE", "5")
    monkeypatch.setenv("RETRY_STUCK_DIALING_S", "600")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")

    s = Settings()

    assert s.retry_poll_interval_s == 15
    assert s.retry_batch_size == 5
    assert s.retry_stuck_dialing_s == 600
    assert s.retry_poller_enabled is False


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("RETRY_POLL_INTERVAL_S", "4"),
        ("RETRY_POLL_INTERVAL_S", "301"),
        ("RETRY_BATCH_SIZE", "0"),
        ("RETRY_BATCH_SIZE", "201"),
        ("RETRY_STUCK_DIALING_S", "119"),
        ("RETRY_STUCK_DIALING_S", "3601"),
    ],
)
def test_retry_settings_out_of_range_rejected(monkeypatch, var, value):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv(var, value)

    with pytest.raises(ValueError, match=var):
        get_settings()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k retry -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'retry_poll_interval_s'`.

- [ ] **Step 3: Add the settings fields**

In `apps/api/src/usan_api/settings.py`, add these fields to the `Settings` class immediately after the `jwt_signing_key` field (line 29):

```python
    retry_poll_interval_s: int = Field(
        default=30, ge=5, le=300, alias="RETRY_POLL_INTERVAL_S"
    )
    retry_batch_size: int = Field(default=20, ge=1, le=200, alias="RETRY_BATCH_SIZE")
    # Must exceed the ring timeout: a genuine in-flight dial leaves DIALING within
    # outbound_ringing_timeout_s, so a row still DIALING past this is stranded.
    retry_stuck_dialing_s: int = Field(
        default=300, ge=120, le=3600, alias="RETRY_STUCK_DIALING_S"
    )
    retry_poller_enabled: bool = Field(default=True, alias="RETRY_POLLER_ENABLED")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k retry -v`
Expected: PASS (8 cases: 2 + 6 parametrized).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/settings.py tests/test_settings.py
git commit -m "feat(api): add retry-orchestrator settings"
```

---

## Task 2: Retry policy (pure function)

**Files:**
- Create: `apps/api/src/usan_api/retry_policy.py`
- Test: `apps/api/tests/test_retry_policy.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_retry_policy.py`:

```python
from datetime import timedelta

import pytest

from usan_api.db.base import CallStatus
from usan_api.retry_policy import next_retry_delay


@pytest.mark.parametrize(
    ("status", "attempt", "expected"),
    [
        (CallStatus.NO_ANSWER, 1, timedelta(minutes=30)),
        (CallStatus.NO_ANSWER, 2, timedelta(hours=2)),
        (CallStatus.NO_ANSWER, 3, None),
        (CallStatus.NO_ANSWER, 4, None),
        (CallStatus.VOICEMAIL_LEFT, 1, timedelta(hours=3)),
        (CallStatus.VOICEMAIL_LEFT, 2, None),
        (CallStatus.BUSY, 1, timedelta(minutes=5)),
        (CallStatus.BUSY, 2, None),
        (CallStatus.FAILED, 1, timedelta(minutes=1)),
        (CallStatus.FAILED, 2, None),
        # out-of-range attempts never produce a delay
        (CallStatus.NO_ANSWER, 0, None),
        (CallStatus.FAILED, 99, None),
    ],
)
def test_next_retry_delay_policy(status, attempt, expected):
    assert next_retry_delay(status, attempt) == expected


@pytest.mark.parametrize(
    "status",
    [
        CallStatus.COMPLETED,
        CallStatus.DNC_BLOCKED,
        CallStatus.CANCELLED,
        CallStatus.QUEUED,
        CallStatus.DIALING,
        CallStatus.RINGING,
        CallStatus.IN_PROGRESS,
    ],
)
def test_non_retryable_statuses_never_retry(status):
    assert next_retry_delay(status, 1) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_retry_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.retry_policy'`.

- [ ] **Step 3: Write the implementation**

Create `apps/api/src/usan_api/retry_policy.py`:

```python
"""§5.3 retry policy (v1 hardcoded).

A retry's delay is keyed on the terminal status and the attempt number that just
ended. ``None`` means stop retrying. Pure function — no I/O, no clock.
"""

from datetime import timedelta

from usan_api.db.base import CallStatus


def next_retry_delay(status: CallStatus, attempt: int) -> timedelta | None:
    """Delay before the next attempt, or None when the policy says stop."""
    if status is CallStatus.NO_ANSWER:
        if attempt == 1:
            return timedelta(minutes=30)
        if attempt == 2:
            return timedelta(hours=2)
        return None
    if status is CallStatus.VOICEMAIL_LEFT:
        return timedelta(hours=3) if attempt == 1 else None
    if status is CallStatus.BUSY:
        return timedelta(minutes=5) if attempt == 1 else None
    if status is CallStatus.FAILED:
        return timedelta(minutes=1) if attempt == 1 else None
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_retry_policy.py -v`
Expected: PASS (19 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/retry_policy.py tests/test_retry_policy.py
git commit -m "feat(api): add §5.3 retry policy"
```

---

## Task 3: Quiet hours (pure function)

**Files:**
- Create: `apps/api/src/usan_api/quiet_hours.py`
- Test: `apps/api/tests/test_quiet_hours.py`

TCPA: a retry may only be placed within `[09:00, 21:00)` in the elder's local time. `next_allowed` returns the earliest aware-UTC instant ≥ `dt_utc` that satisfies this; an invalid timezone raises `ValueError` (the caller fails CLOSED).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_quiet_hours.py`:

```python
from datetime import UTC, datetime

import pytest

from usan_api.quiet_hours import QUIET_END_HOUR, QUIET_START_HOUR, next_allowed


def test_quiet_hour_constants():
    assert QUIET_START_HOUR == 9
    assert QUIET_END_HOUR == 21


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        # before the window -> same-day 09:00
        (datetime(2026, 5, 31, 6, 0, tzinfo=UTC), datetime(2026, 5, 31, 9, 0, tzinfo=UTC)),
        # exactly 09:00 is inside (START inclusive) -> unchanged
        (datetime(2026, 5, 31, 9, 0, tzinfo=UTC), datetime(2026, 5, 31, 9, 0, tzinfo=UTC)),
        # inside the window -> unchanged
        (datetime(2026, 5, 31, 13, 0, tzinfo=UTC), datetime(2026, 5, 31, 13, 0, tzinfo=UTC)),
        # 20:59 still inside -> unchanged
        (datetime(2026, 5, 31, 20, 59, tzinfo=UTC), datetime(2026, 5, 31, 20, 59, tzinfo=UTC)),
        # exactly 21:00 is outside (END exclusive) -> next-day 09:00
        (datetime(2026, 5, 31, 21, 0, tzinfo=UTC), datetime(2026, 6, 1, 9, 0, tzinfo=UTC)),
        # after the window -> next-day 09:00
        (datetime(2026, 5, 31, 23, 0, tzinfo=UTC), datetime(2026, 6, 1, 9, 0, tzinfo=UTC)),
    ],
)
def test_next_allowed_utc_boundaries(base, expected):
    assert next_allowed(base, "UTC") == expected


def test_next_allowed_returns_aware_datetime():
    result = next_allowed(datetime(2026, 5, 31, 6, 0, tzinfo=UTC), "UTC")
    assert result.tzinfo is not None
    assert result.utcoffset() is not None


def test_next_allowed_eastern_before_window_uses_edt_offset():
    # 2026-03-09 is after US spring-forward (2026-03-08): America/New_York is EDT (UTC-4).
    # 06:00 UTC == 02:00 EDT (before 09:00) -> 09:00 EDT == 13:00 UTC.
    base = datetime(2026, 3, 9, 6, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_next_allowed_eastern_before_window_uses_est_offset():
    # 2026-11-02 is after US fall-back (2026-11-01): America/New_York is EST (UTC-5).
    # 06:00 UTC == 01:00 EST (before 09:00) -> 09:00 EST == 14:00 UTC.
    base = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_next_allowed_eastern_after_window_rolls_to_next_local_morning():
    # 2026-03-10 02:00 UTC == 2026-03-09 22:00 EDT (>= 21:00) -> next-day 09:00 EDT == 13:00 UTC.
    base = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 3, 10, 13, 0, tzinfo=UTC)


def test_next_allowed_eastern_inside_window_unchanged():
    # 2026-03-09 17:00 UTC == 13:00 EDT (inside) -> unchanged.
    base = datetime(2026, 3, 9, 17, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == base


@pytest.mark.parametrize("bad_tz", ["Not/AZone", "", "Mars/Phobos"])
def test_next_allowed_invalid_timezone_raises(bad_tz):
    with pytest.raises(ValueError, match="timezone"):
        next_allowed(datetime(2026, 5, 31, 6, 0, tzinfo=UTC), bad_tz)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_quiet_hours.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.quiet_hours'`.

- [ ] **Step 3: Write the implementation**

Create `apps/api/src/usan_api/quiet_hours.py`:

```python
"""TCPA quiet-hours clamping for retry scheduling (§5.3, §10).

A retry may only be placed within ``[09:00, 21:00)`` in the elder's local time.
``next_allowed`` returns the earliest aware-UTC instant >= ``dt_utc`` inside that
window. An invalid IANA timezone raises ValueError so callers can fail CLOSED
(never risk an out-of-hours call) rather than guessing.

Correctness note: zoneinfo.ZoneInfo is a *rule* object — it recomputes the UTC
offset lazily from the wall-clock fields on every access, so building the target
local wall time with ``.replace(hour=9, ...)`` and then ``.astimezone(UTC)`` yields
the correct EST/EDT offset for that date. This is true ONLY for zoneinfo; never
attach a zone with ``.replace(tzinfo=...)`` and never substitute a pytz
bound-offset tzinfo, which would NOT recompute.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

QUIET_START_HOUR = 9  # calls allowed from 09:00 local (inclusive)
QUIET_END_HOUR = 21  # calls not allowed at/after 21:00 local (exclusive)


def next_allowed(dt_utc: datetime, tz_name: str) -> datetime:
    """Earliest aware-UTC instant >= dt_utc within [09:00, 21:00) local time.

    ``dt_utc`` must be timezone-aware. Raises ValueError for an unknown timezone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc

    local = dt_utc.astimezone(tz)
    if QUIET_START_HOUR <= local.hour < QUIET_END_HOUR:
        return dt_utc
    if local.hour < QUIET_START_HOUR:
        target = local.replace(hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    else:  # at/after QUIET_END_HOUR -> next local morning
        target = (local + timedelta(days=1)).replace(
            hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0
        )
    return target.astimezone(UTC)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_quiet_hours.py -v`
Expected: PASS (14 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/quiet_hours.py tests/test_quiet_hours.py
git commit -m "feat(api): add TCPA quiet-hours clamping"
```

---

## Task 4: Migration 0003 — retry indexes

**Files:**
- Create: `apps/api/migrations/versions/0003_retry_indexes.py`
- Test: `apps/api/tests/test_retry_indexes.py`

Two indexes: a partial UNIQUE on `parent_call_id` (at most one retry child per parent — the authoritative idempotency guard), and a tight partial index matching the poller's claim predicate.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_retry_indexes.py`:

```python
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_parent(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        parent = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.NO_ANSWER,
            attempt=1,
        )
        db.add(parent)
        await db.flush()
        await db.commit()
        return parent.id


@pytest.mark.asyncio
async def test_at_most_one_retry_child_per_parent(session_factory):
    parent_id = await _seed_parent(session_factory)

    async with session_factory() as db:
        db.add(
            Call(
                direction=CallDirection.OUTBOUND,
                status=CallStatus.QUEUED,
                parent_call_id=parent_id,
                attempt=2,
            )
        )
        await db.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            db.add(
                Call(
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                    parent_call_id=parent_id,
                    attempt=2,
                )
            )
            await db.commit()


@pytest.mark.asyncio
async def test_null_parent_call_id_not_constrained(session_factory):
    # The unique index is partial (WHERE parent_call_id IS NOT NULL): many rows
    # may have a NULL parent (every initial call), so this must not raise.
    async with session_factory() as db:
        for _ in range(3):
            phone = f"+1555{str(uuid.uuid4().int)[:7]}"
            elder = await elders_repo.create_elder(
                db, name="A", phone_e164=phone, timezone="UTC"
            )
            db.add(
                Call(
                    elder_id=elder.id,
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                )
            )
        await db.commit()  # no IntegrityError
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_retry_indexes.py -v`
Expected: FAIL — the second insert does **not** raise `IntegrityError` (no unique index yet), so `test_at_most_one_retry_child_per_parent` fails.

- [ ] **Step 3: Write the migration**

Create `apps/api/migrations/versions/0003_retry_indexes.py`:

```python
"""retry orchestrator indexes: unique child-per-parent + due-retry partial

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-31

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # At most one retry child per parent: makes schedule_retry's idempotency
    # authoritative (race/replica-safe) instead of advisory.
    op.execute(
        "CREATE UNIQUE INDEX uq_calls_parent_call_id ON calls(parent_call_id) "
        "WHERE parent_call_id IS NOT NULL"
    )
    # Tight match for the poller's claim predicate; excludes NULL-scheduled
    # initial calls so the SKIP LOCKED scan stays small.
    op.execute(
        "CREATE INDEX idx_calls_due_retries ON calls(scheduled_at) "
        "WHERE status = 'queued' AND scheduled_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_due_retries")
    op.execute("DROP INDEX IF EXISTS uq_calls_parent_call_id")
```

- [ ] **Step 4: Run the test to verify it passes**

The session-scoped `database_url` fixture runs `alembic upgrade head` once at the start of the pytest session, so a fresh `pytest` invocation applies 0003.

Run: `cd apps/api && uv run pytest tests/test_retry_indexes.py -v`
Expected: PASS (2 cases).

Also confirm the migration is reversible:

```bash
cd apps/api && uv run alembic history | head
```
Expected: `0003` appears with `down_revision = 0002`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add migrations/versions/0003_retry_indexes.py tests/test_retry_indexes.py
git commit -m "feat(api): add retry-orchestrator indexes (migration 0003)"
```

---

## Task 5: `schedule_retry` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Test: `apps/api/tests/test_retry_scheduling.py`

`schedule_retry(db, call_id)` creates the next-attempt child for a call that just reached a retryable terminal state, in the **caller's** transaction. Idempotent via the partial UNIQUE index (SAVEPOINT + `IntegrityError`). Fails closed on a bad elder timezone.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_retry_scheduling.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

FIXED_NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)  # inside [09:00, 21:00) UTC


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(calls_repo, "_utcnow", lambda: FIXED_NOW)


async def _seed_terminal(factory, *, status, attempt=1, timezone="UTC", dynamic_vars=None):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="A", phone_e164=phone, timezone=timezone
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            dynamic_vars=dynamic_vars or {},
            livekit_room="usan-outbound-parent",
        )
        # create_call defaults attempt via the model; set explicitly for the test
        call.attempt = attempt
        await db.flush()
        await db.commit()
        return call.id, elder.id


async def _child_count(factory, parent_id) -> int:
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_schedule_retry_creates_child(session_factory):
    parent_id, elder_id = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, dynamic_vars={"k": "v"}
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    # re-read in a fresh session to prove it persisted
    async with session_factory() as db:
        reloaded = await calls_repo.get_call(db, child.id)
    assert reloaded is not None
    assert reloaded.parent_call_id == parent_id
    assert reloaded.attempt == 2
    assert reloaded.status is CallStatus.QUEUED
    assert reloaded.elder_id == elder_id
    assert reloaded.dynamic_vars == {"k": "v"}
    assert reloaded.idempotency_key is None
    assert reloaded.livekit_room.startswith("usan-outbound-")
    assert reloaded.livekit_room != "usan-outbound-parent"
    assert reloaded.scheduled_at is not None
    assert reloaded.scheduled_at.tzinfo is not None
    # no_answer attempt 1 -> +30min, inside the UTC window -> exact
    assert reloaded.scheduled_at == FIXED_NOW + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_schedule_retry_is_idempotent(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.BUSY, attempt=1)
    async with session_factory() as db:
        first = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    async with session_factory() as db:
        second = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert first is not None
    assert second is None
    assert await _child_count(session_factory, parent_id) == 1


@pytest.mark.asyncio
async def test_schedule_retry_stops_at_policy_cap(session_factory):
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=3
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_noop_for_non_retryable_status(session_factory):
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.COMPLETED, attempt=1
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_when_elder_missing(session_factory):
    # elder_id is ON DELETE SET NULL, so a parent can legitimately have no elder.
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.FAILED, attempt=1)
    async with session_factory() as db:
        parent = await calls_repo.get_call(db, parent_id)
        parent.elder_id = None
        await db.commit()
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_for_missing_parent(session_factory):
    async with session_factory() as db:
        assert await calls_repo.schedule_retry(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_schedule_retry_fails_closed_on_bad_timezone(session_factory):
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, timezone="Not/AZone"
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_clamps_into_quiet_hours(session_factory):
    # Eastern elder; FIXED_NOW 12:00 UTC == 08:00 EDT (before 09:00 EDT).
    # voicemail_left attempt 1 -> +3h == 15:00 UTC == 11:00 EDT (now inside window) -> exact.
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.VOICEMAIL_LEFT,
        attempt=1,
        timezone="America/New_York",
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(hours=3)
```

> Note: 2026-05-31 is during US DST, so `America/New_York` is EDT (UTC-4): 12:00 UTC = 08:00 EDT, and 15:00 UTC = 11:00 EDT (inside the window), so the +3h result needs no clamping and the assert is exact. This deliberately exercises the Eastern path through `schedule_retry`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v`
Expected: FAIL — `AttributeError: module 'usan_api.repositories.calls' has no attribute 'schedule_retry'`.

- [ ] **Step 3: Write the implementation**

In `apps/api/src/usan_api/repositories/calls.py`, replace the import block at the top (lines 1–9) with:

```python
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import quiet_hours
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
from usan_api.retry_policy import next_retry_delay
```

Then append `schedule_retry` to the end of the file:

```python
async def schedule_retry(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Create the next-attempt child for a call that just reached a retryable
    terminal state (§5.3), in the caller's transaction.

    Returns the child, or None when: the policy says stop, the parent/elder is
    gone, the elder's timezone is invalid (fail CLOSED — never risk a TCPA-hour
    call), or a retry child already exists (idempotent via the partial UNIQUE
    index on parent_call_id).
    """
    parent = await db.get(Call, call_id)
    if parent is None or parent.elder_id is None:
        return None
    delay = next_retry_delay(parent.status, parent.attempt)
    if delay is None:
        return None
    elder = await db.get(Elder, parent.elder_id)
    if elder is None:
        return None
    try:
        scheduled_at = quiet_hours.next_allowed(_utcnow() + delay, elder.timezone)
    except ValueError:
        logger.bind(call_id=str(call_id), timezone=elder.timezone).error(
            "Retry not scheduled: elder timezone is not a valid IANA zone"
        )
        return None

    child = Call(
        elder_id=parent.elder_id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        dynamic_vars=dict(parent.dynamic_vars),
        parent_call_id=parent.id,
        attempt=parent.attempt + 1,
        scheduled_at=scheduled_at,
        livekit_room=f"usan-outbound-{uuid.uuid4()}",
    )
    try:
        async with db.begin_nested():  # SAVEPOINT: a duplicate child rolls back here only
            db.add(child)
            await db.flush()
    except IntegrityError:
        return None  # a sibling attempt already scheduled this retry
    await db.refresh(child)
    return child
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v`
Expected: PASS (8 cases). Also run the full repo suite to confirm no regression:
Run: `cd apps/api && uv run pytest tests/test_calls_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/calls.py tests/test_retry_scheduling.py
git commit -m "feat(api): add schedule_retry (idempotent, quiet-hours-clamped child)"
```

---

## Task 6: `claim_due_retries` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Test: `apps/api/tests/test_retry_orchestrator.py`

`claim_due_retries(db, *, now, limit)` locks and claims up to `limit` due retry rows (`QUEUED` with a past `scheduled_at`), flipping each to `DIALING`. `FOR UPDATE SKIP LOCKED` lets multiple pollers run without ever claiming the same row.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_retry_orchestrator.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory, *, status, scheduled_at, updated_offset_s=None):
    """Insert one call. If updated_offset_s is set, force updated_at to NOW + offset."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            scheduled_at=scheduled_at,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_claim_due_retries_claims_due_queued(session_factory):
    due = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    async with session_factory() as db:
        claimed = await calls_repo.claim_due_retries(db, now=NOW, limit=10)
        await db.commit()
    assert claimed == [due]
    async with session_factory() as db:
        call = await calls_repo.get_call(db, due)
    assert call.status is CallStatus.DIALING


@pytest.mark.asyncio
async def test_claim_skips_not_yet_due(session_factory):
    await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW + timedelta(hours=1)
    )
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_skips_null_scheduled_queued(session_factory):
    # Initial calls (scheduled_at IS NULL) must NEVER be claimed by the poller.
    await _seed(session_factory, status=CallStatus.QUEUED, scheduled_at=None)
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_skips_non_queued(session_factory):
    await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=1)
    )
    await _seed(
        session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=NOW - timedelta(minutes=1)
    )
    async with session_factory() as db:
        assert await calls_repo.claim_due_retries(db, now=NOW, limit=10) == []


@pytest.mark.asyncio
async def test_claim_respects_limit_and_order(session_factory):
    older = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=10)
    )
    newer = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    third = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=5)
    )
    async with session_factory() as db:
        claimed = await calls_repo.claim_due_retries(db, now=NOW, limit=2)
        await db.commit()
    # earliest scheduled_at first; the 2 earliest are `older` then `third`
    assert claimed == [older, third]
    async with session_factory() as db:
        leftover = await calls_repo.get_call(db, newer)
    assert leftover.status is CallStatus.QUEUED  # third row not claimed


@pytest.mark.asyncio
async def test_claim_skips_locked_rows(session_factory, async_database_url):
    due = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        async with session_factory() as db_a:
            claimed_a = await calls_repo.claim_due_retries(db_a, now=NOW, limit=10)
            assert claimed_a == [due]  # A holds the row lock (not committed)
            async with factory_b() as db_b:
                claimed_b = await calls_repo.claim_due_retries(db_b, now=NOW, limit=10)
                assert claimed_b == []  # B skips the locked row instead of blocking
            await db_a.commit()
    finally:
        await engine_b.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -v`
Expected: FAIL — `AttributeError: module 'usan_api.repositories.calls' has no attribute 'claim_due_retries'`.

- [ ] **Step 3: Write the implementation**

Append to `apps/api/src/usan_api/repositories/calls.py`:

```python
async def claim_due_retries(db: AsyncSession, *, now: datetime, limit: int) -> list[uuid.UUID]:
    """Lock and claim up to ``limit`` due retry rows (QUEUED with a past
    scheduled_at), flipping each to DIALING. FOR UPDATE SKIP LOCKED lets multiple
    pollers run without claiming the same row.

    Returns AT MOST ``limit`` ids, and possibly fewer under concurrency (other
    pollers may hold locks on earlier-ordered rows) — never treat an under-full
    batch as "no more work".
    """
    result = await db.execute(
        select(Call)
        .where(
            Call.status == CallStatus.QUEUED,
            Call.scheduled_at.is_not(None),
            Call.scheduled_at <= now,
        )
        .order_by(Call.scheduled_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(result.scalars().all())
    for call in claimed:
        call.status = CallStatus.DIALING
    await db.flush()
    return [call.id for call in claimed]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -v`
Expected: PASS (6 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/calls.py tests/test_retry_orchestrator.py
git commit -m "feat(api): add claim_due_retries (FOR UPDATE SKIP LOCKED)"
```

---

## Task 7: `reclaim_stuck_dialing` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Test: `apps/api/tests/test_retry_orchestrator.py`

Re-queue retry rows stranded in `DIALING` (e.g. the API died mid-dispatch). A genuine in-flight dial leaves `DIALING` within the ring timeout, so a retry row still `DIALING` after `stale_after_s` (>> ring timeout) is stranded. Only retry rows (`scheduled_at IS NOT NULL`) are reclaimed.

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_retry_orchestrator.py`:

```python
async def _force_updated_at(factory, call_id, when):
    from sqlalchemy import update

    async with factory() as db:
        await db.execute(update(Call).where(Call.id == call_id).values(updated_at=when))
        await db.commit()


@pytest.mark.asyncio
async def test_reclaim_requeues_stale_dialing(session_factory):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=600))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(
            db, now=NOW, stale_after_s=300, limit=10
        )
        await db.commit()
    assert reclaimed == [cid]
    async with session_factory() as db:
        call = await calls_repo.get_call(db, cid)
    assert call.status is CallStatus.QUEUED  # now re-claimable by the poller


@pytest.mark.asyncio
async def test_reclaim_leaves_fresh_dialing_alone(session_factory):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=10))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(
            db, now=NOW, stale_after_s=300, limit=10
        )
        await db.commit()
    assert reclaimed == []
    async with session_factory() as db:
        call = await calls_repo.get_call(db, cid)
    assert call.status is CallStatus.DIALING


@pytest.mark.asyncio
async def test_reclaim_ignores_null_scheduled_and_in_progress(session_factory):
    # A stranded INITIAL call (scheduled_at NULL) is the caller's to re-enqueue.
    initial = await _seed(session_factory, status=CallStatus.DIALING, scheduled_at=None)
    await _force_updated_at(session_factory, initial, NOW - timedelta(seconds=600))
    # An answered call is IN_PROGRESS, not DIALING.
    answered = await _seed(
        session_factory, status=CallStatus.IN_PROGRESS, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, answered, NOW - timedelta(seconds=600))
    async with session_factory() as db:
        reclaimed = await calls_repo.reclaim_stuck_dialing(
            db, now=NOW, stale_after_s=300, limit=10
        )
        await db.commit()
    assert reclaimed == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -k reclaim -v`
Expected: FAIL — `AttributeError: ... has no attribute 'reclaim_stuck_dialing'`.

- [ ] **Step 3: Write the implementation**

Append to `apps/api/src/usan_api/repositories/calls.py`:

```python
async def reclaim_stuck_dialing(
    db: AsyncSession, *, now: datetime, stale_after_s: int, limit: int
) -> list[uuid.UUID]:
    """Re-queue retry rows stranded in DIALING (ungraceful death mid-dispatch).

    A genuine in-flight dial leaves DIALING within the ring timeout, so a retry
    row still DIALING after ``stale_after_s`` (>> ring timeout) is stranded. Only
    retry rows (scheduled_at set) are reclaimed; a stranded initial call is the
    caller's to re-enqueue.
    """
    cutoff = now - timedelta(seconds=stale_after_s)
    result = await db.execute(
        select(Call)
        .where(
            Call.status == CallStatus.DIALING,
            Call.scheduled_at.is_not(None),
            Call.updated_at < cutoff,
        )
        .order_by(Call.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stuck = list(result.scalars().all())
    for call in stuck:
        call.status = CallStatus.QUEUED
    await db.flush()
    return [call.id for call in stuck]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -k reclaim -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/calls.py tests/test_retry_orchestrator.py
git commit -m "feat(api): add reclaim_stuck_dialing reaper"
```

---

## Task 8: `mark_failed_if_active` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Test: `apps/api/tests/test_calls_lifecycle.py`

A gated FAILED transition so a crash handler never clobbers a committed `IN_PROGRESS`/`COMPLETED`/`VOICEMAIL_LEFT`.

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_calls_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_mark_failed_if_active_transitions_active(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="mf1")
    async with session_factory() as db:
        call = await calls_repo.mark_failed_if_active(db, call_id, end_reason="internal_error")
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "internal_error"
    assert call.ended_at is not None


@pytest.mark.asyncio
async def test_mark_failed_if_active_noop_when_in_progress(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING, room="mf2")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL")
        await db.commit()
    async with session_factory() as db:
        result = await calls_repo.mark_failed_if_active(db, call_id, end_reason="internal_error")
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS  # not clobbered


@pytest.mark.asyncio
async def test_mark_failed_if_active_noop_when_terminal(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="mf3")
    async with session_factory() as db:
        result = await calls_repo.mark_failed_if_active(db, call_id, end_reason="internal_error")
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_calls_lifecycle.py -k mark_failed_if_active -v`
Expected: FAIL — `AttributeError: ... has no attribute 'mark_failed_if_active'`.

- [ ] **Step 3: Write the implementation**

Append to `apps/api/src/usan_api/repositories/calls.py`:

```python
_ACTIVE_STATUSES = frozenset({CallStatus.QUEUED, CallStatus.DIALING, CallStatus.RINGING})


async def mark_failed_if_active(
    db: AsyncSession, call_id: uuid.UUID, *, end_reason: str
) -> Call | None:
    """Transition a still-active call to FAILED. No-op (returns None) if the call
    already reached IN_PROGRESS or any terminal state, so a crash handler never
    clobbers a committed outcome.
    """
    call = await db.get(Call, call_id)
    if call is None or call.status not in _ACTIVE_STATUSES:
        return None
    call.status = CallStatus.FAILED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_calls_lifecycle.py -k mark_failed_if_active -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/calls.py tests/test_calls_lifecycle.py
git commit -m "feat(api): add mark_failed_if_active gated transition"
```

---

## Task 9: Wire `schedule_retry` into `dial_and_classify`

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py`
- Test: `apps/api/tests/test_livekit_dispatch.py`

The classify path (`no_answer`/`busy`/`failed`) and the gated crash path schedule a retry **in the same transaction** as the terminal mark. The `not_configured` path does NOT (permanent misconfig).

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_livekit_dispatch.py`:

```python
from sqlalchemy import func, select as _select

from usan_api.db.models import Call as _Call


async def _count_children(factory, parent_id):
    async with factory() as db:
        result = await db.execute(
            _select(func.count()).select_from(_Call).where(_Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_dial_busy_schedules_retry(monkeypatch, session_factory):
    fake = _fake_api()
    fake.sip.create_sip_participant.side_effect = _twirp_busy()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-busy-retry")
    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.BUSY
    assert await _count_children(session_factory, call_id) == 1  # busy attempt 1 -> +5min child


@pytest.mark.asyncio
async def test_dial_unconfigured_does_not_schedule_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-noconf-retry")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dial_and_classify(call_id, settings)

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0  # misconfig is permanent


@pytest.mark.asyncio
async def test_dial_crash_marks_failed_and_retries_without_clobbering(monkeypatch, session_factory):
    # Crash AFTER a successful answer must NOT overwrite IN_PROGRESS nor schedule a retry.
    fake = _fake_api()
    fake.sip.create_sip_participant.return_value = MagicMock(sip_call_id="SCL_OK")
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed(session_factory, room="usan-outbound-crash")

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    # Make the inner routine crash AFTER it has marked the call answered/in_progress.
    monkeypatch.setattr(livekit_dispatch, "_dial_and_classify", _boom)
    # First mark it in_progress to simulate "already answered, then crashed".
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_OK")
        await db.commit()

    await livekit_dispatch.dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.IN_PROGRESS  # gated mark did not clobber
    assert await _count_children(session_factory, call_id) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_livekit_dispatch.py -k "retry or clobber" -v`
Expected: FAIL — `test_dial_busy_schedules_retry` finds 0 children (no `schedule_retry` wired in yet).

- [ ] **Step 3: Modify `dial_and_classify` and the not_configured path**

In `apps/api/src/usan_api/livekit_dispatch.py`, replace the guarded `dial_and_classify` (lines 86–101) crash handler so it uses the gated mark + retry:

```python
async def dial_and_classify(call_id: uuid.UUID, settings: Settings) -> None:
    """Background task entrypoint: dial + classify, guarded so an infra failure
    still marks the call FAILED instead of leaving it stuck at ``dialing``."""
    try:
        await _dial_and_classify(call_id, settings)
    except Exception:
        logger.bind(call_id=str(call_id)).exception("dial_and_classify crashed")
        try:
            factory = get_session_factory()
            async with factory() as db:
                failed = await calls_repo.mark_failed_if_active(
                    db, call_id, end_reason="internal_error"
                )
                if failed is not None:
                    await calls_repo.schedule_retry(db, call_id)
                await db.commit()
        except Exception:
            logger.bind(call_id=str(call_id)).warning("Could not mark call FAILED after crash")
```

In `_dial_and_classify`, leave the `not_configured` block unchanged (no retry), and change ONLY the classify-failure block (currently lines 132–143) to schedule a retry in the same transaction:

```python
    try:
        info = await _create_sip_participant(call, elder, settings)
    except Exception as exc:  # busy / no-answer / reject / transport
        status, end_reason, error = classify_dial_exception(exc)
        async with factory() as db:
            await calls_repo.mark_dial_failure(
                db, call_id, status, end_reason=end_reason, error=error
            )
            await calls_repo.schedule_retry(db, call_id)
            await db.commit()
        await _delete_room(room, settings)
        log.info(
            "Outbound dial failed: {status} ({reason})", status=status.value, reason=end_reason
        )
        return
```

> The `not_configured` block (the early `if not settings.livekit_sip_outbound_trunk_id ...`) keeps its existing `mark_dial_failure` + `commit` with **no** `schedule_retry` — misconfig is permanent for the process lifetime.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_livekit_dispatch.py -v`
Expected: PASS (all existing + 3 new).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/livekit_dispatch.py tests/test_livekit_dispatch.py
git commit -m "feat(api): schedule retries from dial failure + gated crash paths"
```

---

## Task 10: `dispatch_and_dial` (poller dispatch entrypoint with DNC re-check)

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py`
- Test: `apps/api/tests/test_dispatch_and_dial.py`

The PULL-side entrypoint the poller spawns per claimed retry. It re-checks DNC at dial time, dispatches the agent (permanent misconfig → FAILED, no retry), then delegates to `dial_and_classify`. A belt-and-suspenders crash guard marks FAILED + schedules a retry.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_dispatch_and_dial.py`:

```python
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
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
        "JWT_SIGNING_KEY": "s" * 32,
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
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_dialing_retry(factory, *, room):
    """A claimed retry row: status=DIALING, scheduled_at set, attempt 2."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            attempt=2,
            livekit_room=room,
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id, phone


async def _count_children(factory, parent_id):
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_dispatch_and_dial_blocks_on_dnc(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, phone = await _seed_dialing_retry(session_factory, room="usan-outbound-dnc")
    async with session_factory() as db:
        await dnc_repo.add_entry(db, phone, "opt-out")
        await db.commit()

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.DNC_BLOCKED
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0  # DNC is terminal, no retry


@pytest.mark.asyncio
async def test_dispatch_and_dial_misconfig_fails_without_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-mc")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dispatch_and_dial(call_id, settings)

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_delegates_to_dial_when_ok(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-ok2")
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v`
Expected: FAIL — `AttributeError: module 'usan_api.livekit_dispatch' has no attribute 'dispatch_and_dial'`.

- [ ] **Step 3: Write the implementation**

In `apps/api/src/usan_api/livekit_dispatch.py`, add `dnc` to the repository imports (the import block near the top currently imports `calls` and `elders`):

```python
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
```

Then append `dispatch_and_dial` to the end of the file:

```python
async def dispatch_and_dial(call_id: uuid.UUID, settings: Settings) -> None:
    """Poller dispatch entrypoint for a claimed retry (already flipped to DIALING).

    Re-checks DNC at dial time (the elder may have opted out since the retry was
    scheduled), dispatches the agent, then delegates to dial_and_classify. A
    permanent misconfig fails the call without a retry; any other crash marks
    FAILED and schedules a retry per §5.3.
    """
    factory = get_session_factory()
    try:
        async with factory() as db:
            call = await calls_repo.get_call(db, call_id)
            if call is None or call.elder_id is None or not call.livekit_room:
                logger.bind(call_id=str(call_id)).warning("dispatch_and_dial: call not dialable")
                return
            elder = await elders_repo.get_elder(db, call.elder_id)
            if elder is None:
                await calls_repo.mark_dial_failure(
                    db, call_id, CallStatus.FAILED, end_reason="elder_missing"
                )
                await db.commit()
                return
            room = call.livekit_room
            # DNC re-check at dial time (closes the schedule->due window).
            await dnc_repo.lock_phone(db, elder.phone_e164)
            blocked = await dnc_repo.is_blocked(db, elder.phone_e164)
            if blocked:
                await calls_repo.set_status(db, call_id, CallStatus.DNC_BLOCKED)
                await db.commit()
                logger.bind(call_id=str(call_id)).info("Retry blocked by DNC")
                await _delete_room(room, settings)
                return
            await db.commit()  # release the advisory lock before the slow dial

        try:
            await dispatch_agent(call, settings=settings)
        except OutboundDispatchError:
            async with factory() as db:
                await calls_repo.mark_dial_failure(
                    db, call_id, CallStatus.FAILED, end_reason="not_configured"
                )
                await db.commit()  # misconfig is permanent — no retry
            await _delete_room(room, settings)
            return

        await dial_and_classify(call_id, settings)
    except Exception:
        logger.bind(call_id=str(call_id)).exception("dispatch_and_dial crashed")
        try:
            async with factory() as db:
                failed = await calls_repo.mark_failed_if_active(
                    db, call_id, end_reason="internal_error"
                )
                if failed is not None:
                    await calls_repo.schedule_retry(db, call_id)
                await db.commit()
        except Exception:
            logger.bind(call_id=str(call_id)).warning("Could not mark retry FAILED after crash")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v`
Expected: PASS (3 cases). Also: `uv run pytest tests/test_livekit_dispatch.py -v` still PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/livekit_dispatch.py tests/test_dispatch_and_dial.py
git commit -m "feat(api): add dispatch_and_dial with DNC re-check for retries"
```

---

## Task 11: Wire `schedule_retry` into the voicemail outcome and enqueue-502 path

**Files:**
- Modify: `apps/api/src/usan_api/routers/calls.py`
- Test: `apps/api/tests/test_calls.py`

The voicemail outcome (gated, exactly-once) and the transient enqueue-dispatch failure (502) each schedule a retry **in the same transaction** as their status change. The permanent misconfig (503) does not.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_calls.py`:

The API has no "list calls" endpoint, so these tests count retry children by querying Postgres directly through a throwaway NullPool engine on `async_database_url`. Add this helper near the top of `tests/test_calls.py` (next to `_service_token`):

```python
def _count_children(async_database_url: str, parent_id: str) -> int:
    import asyncio
    import uuid as _uuid

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.db.models import Call

    async def _run() -> int:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                result = await db.execute(
                    select(func.count())
                    .select_from(Call)
                    .where(Call.parent_call_id == _uuid.UUID(parent_id))
                )
                return result.scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_run())
```

Then the four cases:

```python
def test_outcome_voicemail_schedules_retry(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200
    assert _count_children(async_database_url, call_id) == 1  # voicemail_left attempt 1 -> +3h


def test_outcome_noop_does_not_schedule_retry(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    headers = {"Authorization": f"Bearer {_service_token(call_id)}"}
    # First report marks voicemail_left + schedules the one allowed retry.
    client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"}, headers=headers)
    # A duplicate report is a no-op and must NOT schedule a second retry.
    client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"}, headers=headers)
    assert _count_children(async_database_url, call_id) == 1  # still exactly one child


def test_enqueue_unexpected_dispatch_error_schedules_retry(client, monkeypatch, async_database_url):
    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err502retry", "dynamic_vars": {}},
    )
    assert r.status_code == 502
    # transient dispatch failure -> failed attempt 1 -> +1min child
    assert _count_children(async_database_url, r.json()["id"]) == 1


def test_enqueue_config_error_does_not_schedule_retry(client, monkeypatch, async_database_url):
    async def _raise(*args, **kwargs):
        raise livekit_dispatch.OutboundDispatchError("not configured")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503noretry", "dynamic_vars": {}},
    )
    assert r.status_code == 503
    assert _count_children(async_database_url, r.json()["id"]) == 0  # misconfig is permanent
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_calls.py -k "schedule_retry or schedules_retry or does_not_schedule" -v`
Expected: FAIL — children count is 0 where 1 is expected (retry not wired in).

- [ ] **Step 3: Modify the router**

In `apps/api/src/usan_api/routers/calls.py`, change the generic-Exception branch of `_create_and_dispatch` (currently lines 66–75) to schedule a retry in the same transaction:

```python
    except Exception as exc:
        await calls_repo.set_status(
            db,
            call.id,
            CallStatus.FAILED,
            error={"reason": "dispatch_error", "exc_type": type(exc).__name__},
        )
        await calls_repo.schedule_retry(db, call.id)
        await db.commit()
        logger.bind(call_id=str(call.id)).exception("Agent dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc
```

> Leave the `OutboundDispatchError` branch (the 503 path, just above) unchanged — no `schedule_retry`.

Change `report_outcome` (currently lines 128–145) so the mark and the retry share one commit:

```python
@router.post("/{call_id}/outcome", response_model=CallResponse)
async def report_outcome(
    call_id: uuid.UUID,
    body: CallOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallResponse:
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    # body.outcome is constrained to "voicemail_left"; gate on in_progress so a
    # late/duplicate report never overrides an already-terminal call. The mark and
    # its retry share ONE commit so a crash can't leave a terminal call un-retried.
    updated = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
    if updated is not None:
        await calls_repo.schedule_retry(db, call_id)
    await db.commit()
    logger.bind(call_id=str(call_id)).info("Call outcome reported: {o}", o=body.outcome)
    return CallResponse.from_model(updated or call)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_calls.py -v`
Expected: PASS (all existing + 4 new).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/routers/calls.py tests/test_calls.py
git commit -m "feat(api): schedule retries from voicemail outcome + transient enqueue failure"
```

---

## Task 12: Retry orchestrator (`poll_once` + `run_poller`)

**Files:**
- Create: `apps/api/src/usan_api/retry_orchestrator.py`
- Test: `apps/api/tests/test_retry_orchestrator.py`

`poll_once` reaps stuck `dialing` rows, claims due retries, and spawns `dispatch_and_dial` per claimed id (after committing the claim). `run_poller` loops `poll_once` on the configured interval, surviving per-cycle exceptions, and exits promptly when its stop event is set.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_retry_orchestrator.py` (it already imports the basics; add these imports at the top of the new block and the body):

```python
import asyncio

from usan_api import background, retry_orchestrator
from usan_api.settings import Settings


@pytest.fixture(autouse=True)
def _clear_background_tasks():
    background._tasks.clear()
    yield
    background._tasks.clear()


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_poll_once_claims_commits_and_dispatches(session_factory, monkeypatch):
    due1 = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=2)
    )
    due2 = await _seed(
        session_factory, status=CallStatus.QUEUED, scheduled_at=NOW - timedelta(minutes=1)
    )

    dispatched: list[uuid.UUID] = []

    async def _fake_dispatch(call_id, settings):
        dispatched.append(call_id)

    monkeypatch.setattr(retry_orchestrator.livekit_dispatch, "dispatch_and_dial", _fake_dispatch)

    ids = await retry_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    await background.drain(timeout=5)

    assert set(ids) == {due1, due2}
    assert set(dispatched) == {due1, due2}
    # claim was committed before dispatch: rows are DIALING in a fresh session
    async with session_factory() as db:
        for cid in (due1, due2):
            call = await calls_repo.get_call(db, cid)
            assert call.status is CallStatus.DIALING

    # a second poll claims nothing (rows already DIALING)
    ids2 = await retry_orchestrator.poll_once(session_factory, _settings(), now=NOW)
    assert ids2 == []


@pytest.mark.asyncio
async def test_poll_once_reaps_then_claims_stuck_row(session_factory, monkeypatch):
    cid = await _seed(
        session_factory, status=CallStatus.DIALING, scheduled_at=NOW - timedelta(minutes=20)
    )
    await _force_updated_at(session_factory, cid, NOW - timedelta(seconds=600))

    dispatched: list[uuid.UUID] = []

    async def _fake_dispatch(call_id, settings):
        dispatched.append(call_id)

    monkeypatch.setattr(retry_orchestrator.livekit_dispatch, "dispatch_and_dial", _fake_dispatch)

    ids = await retry_orchestrator.poll_once(
        session_factory, _settings(RETRY_STUCK_DIALING_S="300"), now=NOW
    )
    await background.drain(timeout=5)
    # the stranded row was reaped to QUEUED, then claimed and dispatched in one cycle
    assert ids == [cid]
    assert dispatched == [cid]


@pytest.mark.asyncio
async def test_run_poller_exits_promptly_when_stop_preset(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)
    calls_count = {"n": 0}

    async def _fake_poll(factory, settings, *, now=None):
        calls_count["n"] += 1
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(retry_orchestrator.run_poller(_settings(), stop), timeout=2)
    assert calls_count["n"] == 0  # stop preset -> never polls, never sleeps the interval


@pytest.mark.asyncio
async def test_run_poller_survives_poll_exception(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)
    stop = asyncio.Event()
    n = {"i": 0}

    async def _flaky(factory, settings, *, now=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("transient boom")
        stop.set()
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _flaky)

    settings = _settings()
    settings.retry_poll_interval_s = 0.01  # Settings has no validate_assignment; spin fast
    await asyncio.wait_for(retry_orchestrator.run_poller(settings, stop), timeout=2)
    assert n["i"] >= 2  # exception in cycle 1 did not kill the loop


@pytest.mark.asyncio
async def test_run_poller_stop_interrupts_interval_sleep(monkeypatch):
    monkeypatch.setattr(retry_orchestrator, "get_session_factory", lambda: None)

    async def _fake_poll(factory, settings, *, now=None):
        return []

    monkeypatch.setattr(retry_orchestrator, "poll_once", _fake_poll)

    stop = asyncio.Event()
    settings = _settings()  # default 30s interval
    task = asyncio.create_task(retry_orchestrator.run_poller(settings, stop))
    await asyncio.sleep(0.05)  # let one cycle run and enter the interval sleep
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # must return well before 30s
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -k "poll_once or run_poller" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.retry_orchestrator'`.

- [ ] **Step 3: Write the implementation**

Create `apps/api/src/usan_api/retry_orchestrator.py`:

```python
"""In-process retry orchestrator (§4.1 RetryOrchestrator, §5.3 policy).

A single async loop per process. Each cycle: reap rows stranded in DIALING, claim
due retry rows with FOR UPDATE SKIP LOCKED, and dispatch each as a tracked
background task. Multiple replicas may each run this safely — SKIP LOCKED and the
partial UNIQUE index on parent_call_id make claiming and scheduling idempotent.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import background, livekit_dispatch
from usan_api.db.session import get_session_factory
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """One poll cycle: reap stranded DIALING rows, claim due retries, dispatch.

    Returns the claimed ids (at most ``retry_batch_size``). The claim is committed
    before dispatch is spawned, so a spawned dispatch always sees DIALING.
    """
    moment = now or _utcnow()
    async with factory() as db:
        await calls_repo.reclaim_stuck_dialing(
            db,
            now=moment,
            stale_after_s=settings.retry_stuck_dialing_s,
            limit=settings.retry_batch_size,
        )
        await db.commit()
    async with factory() as db:
        claimed = await calls_repo.claim_due_retries(
            db, now=moment, limit=settings.retry_batch_size
        )
        await db.commit()
    for call_id in claimed:
        background.spawn(livekit_dispatch.dispatch_and_dial(call_id, settings))
    return claimed


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop poll_once on the configured interval until ``stop`` is set.

    Survives per-cycle exceptions (logged, never fatal). The interval sleep is a
    cancellable wait on ``stop``, so shutdown is prompt.
    """
    log = logger.bind(component="retry_poller")
    log.info("Retry poller started (interval={i}s)", i=settings.retry_poll_interval_s)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            claimed = await poll_once(factory, settings)
            if claimed:
                log.info("Dispatched {n} due retry call(s)", n=len(claimed))
        except Exception:
            log.opt(exception=True).error("Retry poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.retry_poll_interval_s)
    log.info("Retry poller stopped")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_retry_orchestrator.py -v`
Expected: PASS (all cases — claim/reclaim/poll_once/run_poller).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/retry_orchestrator.py tests/test_retry_orchestrator.py
git commit -m "feat(api): add retry poller (poll_once + run_poller)"
```

---

## Task 13: Start/stop the poller in the FastAPI lifespan

**Files:**
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_lifespan_poller.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_lifespan_poller.py`:

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from usan_api import retry_orchestrator
from usan_api.main import create_app
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


def _set_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lifespan_starts_and_stops_poller(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "true")
    state: dict = {"started": False, "stop": None}

    async def _fake_run_poller(settings, stop):
        state["started"] = True
        state["stop"] = stop
        await stop.wait()  # block until shutdown signals stop

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_run_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert state["started"] is True

    assert isinstance(state["stop"], asyncio.Event)
    assert state["stop"].is_set()  # shutdown set the stop event


def test_lifespan_skips_poller_when_disabled(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    started = {"v": False}

    async def _fake_run_poller(settings, stop):
        started["v"] = True
        await stop.wait()

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_run_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200

    assert started["v"] is False  # poller never started
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v`
Expected: FAIL — `state["started"]` is `False` (lifespan doesn't start the poller yet).

- [ ] **Step 3: Modify the lifespan**

Replace the top of `apps/api/src/usan_api/main.py` (the imports and `lifespan`) with:

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from pydantic import BaseModel

from usan_api import background, retry_orchestrator
from usan_api.db.session import dispose_engine
from usan_api.logging_config import configure_logging
from usan_api.routers import calls, dnc, elders, webhooks
from usan_api.settings import get_settings


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = asyncio.Event()
    poller_task: asyncio.Task[None] | None = None
    if settings.retry_poller_enabled:
        poller_task = asyncio.create_task(retry_orchestrator.run_poller(settings, stop))
    try:
        yield
    finally:
        stop.set()
        if poller_task is not None:
            poller_task.cancel()
            with suppress(asyncio.CancelledError):
                await poller_task
        # Drain longer than the longest blocking dial (ringing timeout) so an
        # in-flight dial finishes and writes its outcome before the engine closes;
        # otherwise it would write against a disposed engine and stick at 'dialing'.
        drain_timeout = float(settings.outbound_ringing_timeout_s) + 15.0
        await background.drain(timeout=drain_timeout)
        await dispose_engine()
```

Leave `create_app()` unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/main.py tests/test_lifespan_poller.py
git commit -m "feat(api): start/stop retry poller in the FastAPI lifespan"
```

---

## Task 14: Infra config + docs

**Files:**
- Modify: `infra/.env.example`
- Modify: `infra/docker-compose.yml`
- Modify: `infra/README.md`

No tests (config + docs). Document the new env vars, the §10 quiet-hours scope decision, and the multi-replica poller behavior.

- [ ] **Step 1: Add the env vars to `infra/.env.example`**

Append to `infra/.env.example`:

```bash
# === Retry orchestrator (in-process Postgres poller) ===
# Re-dials no_answer/voicemail_left/busy/failed calls per the §5.3 policy, gated
# by TCPA quiet hours (09:00-21:00 in the elder's local timezone). All optional;
# defaults shown.
# Seconds between poll cycles (5-300).
# RETRY_POLL_INTERVAL_S=30
# Max rows claimed per cycle (1-200).
# RETRY_BATCH_SIZE=20
# Re-queue a retry stranded in 'dialing' after this many seconds (must exceed the
# ring timeout; 120-3600).
# RETRY_STUCK_DIALING_S=300
# Set to false to disable the poller on a given replica (e.g. run it on only one).
# RETRY_POLLER_ENABLED=true
```

- [ ] **Step 2: Surface the toggle on the api service in `infra/docker-compose.yml`**

In the `api` service's `environment:` block, add (only the enable toggle and interval need surfacing; the rest use defaults):

```yaml
      RETRY_POLLER_ENABLED: ${RETRY_POLLER_ENABLED:-true}
      RETRY_POLL_INTERVAL_S: ${RETRY_POLL_INTERVAL_S:-30}
```

> Match the existing indentation and `${VAR:-default}` style already used for other api env vars in that file. Do not add the vars anywhere else.

- [ ] **Step 3: Document the orchestrator in `infra/README.md`**

Add a section to `infra/README.md`:

```markdown
## Retry orchestrator

The API runs an in-process poller (`retry_orchestrator.run_poller`) that re-dials
calls per the §5.3 policy:

| End state | Retries |
|---|---|
| `no_answer` | +30 min, then +2 h (3 attempts total) |
| `voicemail_left` | +3 h (2 attempts total) |
| `busy` | +5 min (2 attempts total) |
| `failed` (transport) | +1 min (2 attempts total) |

Each retry is a new `calls` row linked to its predecessor by `parent_call_id`
(`attempt = parent.attempt + 1`). A partial UNIQUE index on `parent_call_id`
guarantees at most one retry per attempt. DNC is re-checked at dial time.

**TCPA quiet hours:** retries are never placed before 09:00 or at/after 21:00 in
the elder's local timezone. An invalid elder timezone fails CLOSED (the retry is
not scheduled and an ERROR is logged) rather than risking an out-of-hours call.

**Initial calls** (`POST /v1/calls`) are NOT quiet-hours gated and dial
immediately — the upstream scheduler owns quiet hours for the first attempt
(spec §10 scopes orchestrator enforcement to retries).

**Multiple replicas:** each replica runs its own poller. `FOR UPDATE SKIP LOCKED`
plus the `parent_call_id` unique index make claiming and scheduling safe without
leader election. Set `RETRY_POLLER_ENABLED=false` to confine the poller to a
single replica if preferred.

A stuck-`dialing` reaper re-queues retry rows left in `dialing` by an ungraceful
process death after `RETRY_STUCK_DIALING_S` (must exceed the ring timeout).
```

- [ ] **Step 4: Validate compose**

Run: `cd infra && docker compose config >/dev/null && echo OK`
Expected: `OK` (compose file parses).

- [ ] **Step 5: Commit**

```bash
git add infra/.env.example infra/docker-compose.yml infra/README.md
git commit -m "docs(infra): document retry orchestrator + quiet-hours config"
```

---

## Self-Review

**1. Spec coverage (§5.3, §5.2, §10):**

- `no_answer` +30m/+2h/stop, `voicemail_left` +3h/stop, `busy` +5m/stop, `failed` +1m/stop → Task 2 (`next_retry_delay`), exhaustively tested.
- New-row chaining via `parent_call_id` + `attempt+1` → Task 5 (`schedule_retry`).
- Retry scheduled from every terminal site: dial classify + crash (Task 9), poller dispatch crash (Task 10), voicemail outcome + transient enqueue failure (Task 11). Permanent misconfig (503 / `not_configured`) excluded — Tasks 9, 10, 11.
- TCPA `[09:00, 21:00)` local gating → Task 3 (`next_allowed`), applied in Task 5; fail-closed on bad tz.
- DNC re-check before any retry dial → Task 10.
- "no_answer terminal at max_attempts" → policy returns `None` at attempt 3 (Task 2) → no child (Task 5).
- Durability (stranded `dialing`) → reaper Tasks 7 + 12.
- Idempotency (no double-dial) → unique index Task 4 + SAVEPOINT Task 5 + gated voicemail mark Task 11.

**2. Placeholder scan:** Clean. Every code block is complete and runnable as written — no "TBD"/"implement later"/"handle errors"/"similar to Task N", and no stub functions. Task 11's `_count_children` is a real helper (the API has no list endpoint, so children are counted directly in Postgres); Task 12 ships the final imports and `-> list[uuid.UUID]` signature.

**3. Type consistency:** Signatures are stable across tasks — `next_retry_delay(status, attempt)`, `next_allowed(dt_utc, tz_name)`, `schedule_retry(db, call_id)`, `claim_due_retries(db, *, now, limit)`, `reclaim_stuck_dialing(db, *, now, stale_after_s, limit)`, `mark_failed_if_active(db, call_id, *, end_reason)`, `dispatch_and_dial(call_id, settings)`, `poll_once(factory, settings, *, now=None)`, `run_poller(settings, stop)`. Settings aliases match the env-var names used in compose/.env and tests. `CallStatus`/`CallDirection` members match `db/base.py`.

**Lint watch-outs encoded in the plan:** no `try/except/pass` (S110) — the poller logs cycle failures and uses `contextlib.suppress(TimeoutError)` for the interval sleep (SIM105); no `async def` declares a `timeout` parameter (ASYNC109); `except Exception:` is allowed (BLE not selected). All new `src` signatures are fully typed for `mypy --strict`.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-31-plan-2b-3-retry-orchestrator.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, two-stage review (spec compliance then code quality) between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Note:** This plan builds directly on Plan 2b-2's code. Execute it from a worktree branched off a `main` that already contains the merged 2b-2 (PR #5), so `schedule_retry` can see `mark_voicemail_left_if_in_progress` and the JWT-authenticated outcome endpoint.
