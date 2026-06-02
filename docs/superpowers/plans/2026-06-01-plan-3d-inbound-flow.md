# Plan 3d — Inbound Call Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make inbound calls a first-class, personalized check-in — when an elder dials our Telnyx number, the agent looks them up by caller-ID, the API mints an inbound `calls` row, and the agent runs the same tool-driven wellness check-in (with the elder's name + last check-in injected) and flushes the transcript — instead of today's anonymous greet-only response.

**Architecture:** Agent-initiated lookup (spec §3 step 3). The inbound SIP trunk + dispatch rule already route the call to a metadata-less agent job, which `parse_metadata` already treats as inbound. On inbound the agent waits for the caller, reads the SIP caller-ID attribute (`sip.phoneNumber`), and POSTs it to a new worker-token-authed `POST /v1/calls/inbound`. The API looks the elder up by phone, creates an answered `direction=inbound` call (`elder_id` nullable — unknown numbers still get a record), computes `dynamic_vars` (name + last check-in), and returns `{call_id, elder_known, dynamic_vars}`. A known elder → the agent builds an inbound check-in agent (the four existing in-call tools, personalized instructions), registers the transcript flush, starts the session, and opens with a personalized greeting. An unknown/failed lookup → the existing greet-only path. Call lifecycle (room_finished → COMPLETED, `end_call` tool) and tool auth already work for inbound unchanged.

**Tech Stack:** apps/api (FastAPI, SQLAlchemy async, Pydantic, PyJWT) + services/agent (livekit-agents 1.5.14 `JobContext.wait_for_participant`/`RemoteParticipant.attributes`, `AgentSession.generate_reply`; httpx; PyJWT).

---

## Context for the implementer

This plan spans **both services** in one worktree. Sync and run each independently:

```bash
cd apps/api && uv sync && uv run pytest -v && uv run ruff check . && uv run ruff format . && uv run mypy
cd services/agent && uv sync && uv run pytest -v && uv run ruff check . && uv run ruff format . && uv run mypy
```

Conventions (both services): ruff `["E","F","I","B","UP","ASYNC","S","PT","RET","SIM"]`, line 100, `S` ignored in `tests/**`; mypy `--strict` on `src`; `asyncio_mode=auto` (agent) / pytest with testcontainers (api); commit `type(scope): description` (scope `api` or `agent`), **no `Co-Authored-By`**. `services/agent` must NOT import `apps/api` (HTTP only).

### Verified facts (current `main`: Plans 1–3c + livekit-agents 1.5.14)

- **Inbound routing already exists.** `infra/livekit-sip-trunk.json` is the inbound trunk (`numbers: ["${TELNYX_INBOUND_DID}"]`); `infra/livekit-sip-dispatch-rule.json` dispatches `agent_name: "usan-agent"` into `usan-inbound-` rooms. Dispatch-rule jobs carry **no** `job.metadata`, and `worker.parse_metadata(None)` already returns `CallMetadata(call_id=None, direction="inbound", dynamic_vars={})`. **No infra routing change is needed** — only the agent's inbound *behavior* changes.
- **No DB migration is needed.** The `calls` table, `transcripts`/`wellness_logs`/`medication_logs`, and the `call_direction` enum (which already includes `'inbound'`, see `apps/api/src/usan_api/db/base.py:10-12`) already exist. Inbound reuses them.
- **Auth (`apps/api/src/usan_api/auth.py`):** `require_service_token` decodes HS256 with `options={"require": ["exp", "call_id"]}` — it **hard-requires a `call_id` claim**. On inbound there is no call yet, so the agent cannot present a call-scoped token to create one. This plan adds `require_worker_token` (requires only `exp`).
- **Agent JWT (`services/agent/src/usan_agent/api_client.py`):** `_mint_token(call_id, settings)` (HS256, `_TOKEN_TTL_S=300`, claims `sub/call_id/iat/exp`); `_post_tool` is the call-scoped client; `report_voicemail_left`/`flush_transcript` are the **best-effort** POST pattern (swallow all exceptions). This plan adds `_mint_worker_token` (no `call_id`) and `start_inbound_call` (best-effort, returns parsed JSON or `None`).
- **Tool endpoints + auth already work for any call with an elder.** `routers/tools.py` `_authorize_call(call_id, claims, db)` asserts `claims["call_id"] == str(call_id)` then loads the call; `_require_elder(call)` 409s if `elder_id is None`. So once the agent has an inbound `call_id` + a call-scoped token, `log_wellness`/`log_medication`/`get_today_meds`/`end_call`/`log_transcript` work unchanged.
- **Lifecycle already closes inbound calls.** `routers/webhooks.py` → `calls_repo.mark_completed_if_in_progress(db, room_name)` matches the call by `livekit_room` and computes `duration_seconds` from `answered_at`, regardless of direction or `elder_id`. So an inbound call set to `IN_PROGRESS` with `answered_at` is closed on `room_finished` for free. The `end_call` tool (`complete_call_if_in_progress`, gated on `IN_PROGRESS`) also works for inbound.
- **Worker entrypoint (`services/agent/src/usan_agent/worker.py`):** today the outbound branch builds the check-in agent and the `else` (inbound) branch builds a greet-only `build_agent()` then `await ctx.wait_for_participant()` + `await greet(session)`. This plan moves `session.start` + the participant wait **into** the outbound branch (behavior-identical) and replaces the inbound tail with `_run_inbound(...)`.
- **LiveKit 1.5.14 APIs (verified in the installed package):** `JobContext.wait_for_participant(...) -> rtc.RemoteParticipant`; `RemoteParticipant.attributes -> dict[str, str]`; `AgentSession.say(text, ...)` and `AgentSession.generate_reply(*, instructions=..., ...)` both exist. livekit-sip populates the SIP participant's `attributes["sip.phoneNumber"]` with the remote party's E.164 number (the caller on inbound). `FunctionTool` has **no `.name`**, use `.id` (== function name).

### Decisions locked

1. **Agent-initiated lookup, not a LiveKit room webhook.** Spec §3 step 3 says "the agent fires a webhook to apps/api for caller-ID → elder lookup"; §4.1 sketches a `POST /webhooks/livekit/room`. We implement §3: the agent holds the caller-ID (from the SIP participant) and needs the result (`call_id` + dynamic vars) **synchronously**, and this reuses the existing agent→API JWT pattern. A LiveKit room webhook would force the agent to poll/look-up-by-room. The endpoint is `POST /v1/calls/inbound` on the calls router, authed by the new `require_worker_token`.
2. **The API always creates an inbound `calls` row**, even for an unknown/absent caller-ID (`elder_id = NULL`), so inbound attempts are recorded. `status = IN_PROGRESS`, `answered_at = started_at = now` (an inbound call is answered by definition).
3. **Known elder → full check-in; unknown/failed → greet-only.** A known elder gets `build_inbound_agent(dynamic_vars)` (the four existing tools + personalized instructions), `register_transcript_flush`, and a `generate_reply` opening. An unknown caller (or a failed/None lookup) falls back to today's `build_agent()` + `greet()` — no per-elder state, no tools, no flush — avoiding orphaned `wellness_logs`/`medication_logs` (which require a non-null `elder_id`).
4. **`dynamic_vars` (spec §3) = `{elder_name, last_check_in?}`.** `last_check_in` is a short human string built from the elder's most recent `wellness_logs` row (date + mood + pain + note), omitted when there is none. "Today's meds" is **not** pre-injected — the agent fetches it live via the existing `get_today_meds` tool. Persisted into the call's `dynamic_vars` JSONB for the record.
5. **No DNC check on inbound** — DNC governs outbound dialing only.
6. **Best-effort lookup with greet-only fallback.** `start_inbound_call` swallows errors and returns `None`; the worker then runs the greet-only path so a transient API outage never drops an inbound caller.

---

## File Structure

**Create:**
- `apps/api/tests/test_inbound_repo.py` — repo tests (`get_elder_by_phone`, `create_inbound_call`, `get_latest_for_elder`).
- `apps/api/tests/test_inbound.py` — `POST /v1/calls/inbound` endpoint tests.

**Modify:**
- `apps/api/src/usan_api/auth.py` — add `require_worker_token`.
- `apps/api/tests/test_auth.py` — worker-token tests.
- `apps/api/src/usan_api/repositories/elders.py` — add `get_elder_by_phone`.
- `apps/api/src/usan_api/repositories/calls.py` — add `create_inbound_call`.
- `apps/api/src/usan_api/repositories/wellness.py` — add `get_latest_for_elder`.
- `apps/api/src/usan_api/schemas/call.py` — add `InboundCallRequest`, `InboundCallResponse`.
- `apps/api/src/usan_api/routers/calls.py` — add `POST /v1/calls/inbound` + `_format_last_check_in`.
- `services/agent/src/usan_agent/api_client.py` — add `_mint_worker_token`, `start_inbound_call`.
- `services/agent/tests/test_api_client.py` — `start_inbound_call` tests.
- `services/agent/src/usan_agent/check_in.py` — add `INBOUND_INSTRUCTIONS_TEMPLATE`, `_inbound_instructions`, `build_inbound_agent`.
- `services/agent/tests/test_check_in.py` — inbound-agent tests.
- `services/agent/src/usan_agent/worker.py` — `_caller_phone`, `_run_inbound`, entrypoint restructure.
- `services/agent/tests/test_worker.py` — replace the two inbound tests with new inbound-behavior tests.
- `infra/README.md` — update the inbound smoke test to the new behavior.

---

## Task 1: API — `require_worker_token`

**Files:**
- Modify: `apps/api/src/usan_api/auth.py`
- Test: `apps/api/tests/test_auth.py`

Work in `apps/api`.

- [ ] **Step 1: Write the failing tests**

In `apps/api/tests/test_auth.py`, add the `require_worker_token` import and a `/worker` route on the existing test app, plus tests. Change the import line at the top:

```python
from usan_api.auth import require_service_token, require_worker_token
```

Add a `/worker` route inside the existing `_app()` function (right before `return app`):

```python
    @app.get("/worker")
    def worker(claims: dict = Depends(require_worker_token)) -> dict:
        return {"sub": claims.get("sub")}
```

Append these tests to the end of the file:

```python
def _worker_token_str(secret: str = SECRET, *, exp_delta: int = 300) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + exp_delta}, secret, algorithm="HS256"
    )


def test_worker_token_without_call_id_accepted(auth_client):
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {_worker_token_str()}"})
    assert r.status_code == 200


def test_worker_token_missing_401(auth_client):
    assert auth_client.get("/worker").status_code == 401


def test_worker_token_wrong_secret_401(auth_client):
    bad = _worker_token_str(secret="x" * 32)
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_worker_token_expired_401(auth_client):
    expired = _worker_token_str(exp_delta=-10)
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_worker_token_accepts_call_scoped_token(auth_client):
    # A call-scoped token (with call_id) is also valid — we only require exp.
    r = auth_client.get("/worker", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'require_worker_token'`.

- [ ] **Step 3: Implement `require_worker_token`**

Append to `apps/api/src/usan_api/auth.py`:

```python
def require_worker_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a worker JWT that is NOT yet scoped to a specific call.

    Inbound calls have no call_id until the API mints one, so the agent cannot
    present a call-scoped token to the inbound-create endpoint. This verifies the
    HS256 signature + exp only; it proves the caller holds JWT_SIGNING_KEY (our
    agent worker). Endpoints using it CREATE a resource rather than mutate a named
    one; for mutating an existing call, use require_service_token.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    try:
        return jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token"
        ) from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_auth.py -v`
Expected: PASS (all worker-token tests green; existing `require_service_token` tests still pass).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/auth.py tests/test_auth.py
git commit -m "feat(api): add require_worker_token for not-yet-scoped agent calls"
```

---

## Task 2: API — repositories (`get_elder_by_phone`, `create_inbound_call`, `get_latest_for_elder`)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/elders.py`, `apps/api/src/usan_api/repositories/calls.py`, `apps/api/src/usan_api/repositories/wellness.py`
- Test: `apps/api/tests/test_inbound_repo.py`

Work in `apps/api`.

- [ ] **Step 1: Write the failing repo tests**

Create `apps/api/tests/test_inbound_repo.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import wellness as wellness_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


@pytest.mark.asyncio
async def test_get_elder_by_phone_found_and_missing(session_factory):
    phone = _phone()
    async with session_factory() as db:
        elder = await elders_repo.create_elder(db, name="Ada", phone_e164=phone, timezone="UTC")
        await db.commit()
        eid = elder.id
    async with session_factory() as db:
        found = await elders_repo.get_elder_by_phone(db, phone)
        missing = await elders_repo.get_elder_by_phone(db, "+10000000000")
    assert found is not None
    assert found.id == eid
    assert missing is None


@pytest.mark.asyncio
async def test_create_inbound_call_is_answered_in_progress(session_factory):
    phone = _phone()
    async with session_factory() as db:
        elder = await elders_repo.create_elder(db, name="Ada", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_inbound_call(
            db,
            elder_id=elder.id,
            livekit_room="usan-inbound-r1",
            dynamic_vars={"elder_name": "Ada"},
        )
        await db.commit()
    assert call.direction is CallDirection.INBOUND
    assert call.status is CallStatus.IN_PROGRESS
    assert call.answered_at is not None
    assert call.started_at is not None
    assert call.livekit_room == "usan-inbound-r1"
    assert call.dynamic_vars == {"elder_name": "Ada"}


@pytest.mark.asyncio
async def test_create_inbound_call_allows_null_elder(session_factory):
    async with session_factory() as db:
        call = await calls_repo.create_inbound_call(
            db, elder_id=None, livekit_room="usan-inbound-r2"
        )
        await db.commit()
    assert call.elder_id is None
    assert call.direction is CallDirection.INBOUND
    assert call.dynamic_vars == {}


@pytest.mark.asyncio
async def test_get_latest_for_elder_returns_most_recent(session_factory):
    phone = _phone()
    async with session_factory() as db:
        elder = await elders_repo.create_elder(db, name="Ada", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_inbound_call(
            db, elder_id=elder.id, livekit_room="usan-inbound-r3"
        )
        await db.commit()
        assert await wellness_repo.get_latest_for_elder(db, elder.id) is None
        await wellness_repo.create_wellness_log(
            db, call_id=call.id, elder_id=elder.id, mood=3, pain_level=2, notes="ok"
        )
        await wellness_repo.create_wellness_log(
            db, call_id=call.id, elder_id=elder.id, mood=5, pain_level=0, notes="great"
        )
        await db.commit()
        latest = await wellness_repo.get_latest_for_elder(db, elder.id)
    assert latest is not None
    assert latest.mood == 5  # tie-broken by id desc (both share the txn's now())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_inbound_repo.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'get_elder_by_phone'` (and `create_inbound_call`, `get_latest_for_elder`).

- [ ] **Step 3a: Implement `get_elder_by_phone`**

In `apps/api/src/usan_api/repositories/elders.py`, add the `select` import and the function. Change the imports block to:

```python
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Elder
```

Append the function:

```python
async def get_elder_by_phone(db: AsyncSession, phone_e164: str) -> Elder | None:
    """Look up an elder by E.164 phone (UNIQUE) — the inbound caller-ID lookup."""
    result = await db.execute(select(Elder).where(Elder.phone_e164 == phone_e164))
    return result.scalar_one_or_none()
```

- [ ] **Step 3b: Implement `create_inbound_call`**

Append to `apps/api/src/usan_api/repositories/calls.py` (it already imports `uuid`, `Any`, `Call`, `CallDirection`, `CallStatus`, and defines `_utcnow`):

```python
async def create_inbound_call(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID | None,
    livekit_room: str,
    sip_call_id: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    """Create an answered inbound call (IN_PROGRESS, answered now).

    Inbound calls are answered by definition (the caller is on the line), so
    started_at/answered_at are set immediately; the room_finished webhook later
    marks COMPLETED and computes duration_seconds from answered_at. elder_id may
    be NULL for an unknown caller — the row still records the inbound attempt.
    """
    now = _utcnow()
    call = Call(
        elder_id=elder_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        livekit_room=livekit_room,
        sip_call_id=sip_call_id,
        dynamic_vars=dynamic_vars or {},
        started_at=now,
        answered_at=now,
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 3c: Implement `get_latest_for_elder`**

In `apps/api/src/usan_api/repositories/wellness.py`, add the `select` import and the function. Change the imports block to:

```python
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WellnessLog
```

Append the function:

```python
async def get_latest_for_elder(db: AsyncSession, elder_id: uuid.UUID) -> WellnessLog | None:
    """The elder's most recent wellness log (for the inbound 'last check-in' var).

    Ordered by logged_at then id descending so a tie on logged_at (rows written in
    one transaction share now()) resolves deterministically to the newest insert.
    """
    result = await db.execute(
        select(WellnessLog)
        .where(WellnessLog.elder_id == elder_id)
        .order_by(WellnessLog.logged_at.desc(), WellnessLog.id.desc())
        .limit(1)
    )
    return result.scalars().first()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_inbound_repo.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/repositories/elders.py src/usan_api/repositories/calls.py \
        src/usan_api/repositories/wellness.py tests/test_inbound_repo.py
git commit -m "feat(api): inbound repo helpers (elder-by-phone, inbound call, latest wellness)"
```

---

## Task 3: API — `POST /v1/calls/inbound` endpoint

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py`, `apps/api/src/usan_api/routers/calls.py`
- Test: `apps/api/tests/test_inbound.py`

Work in `apps/api`.

- [ ] **Step 1: Write the failing endpoint tests**

Create `apps/api/tests/test_inbound.py`:

```python
import time
import uuid

import jwt

SECRET = "s" * 32


def _worker_token(secret: str = SECRET) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def _service_token(call_id: str, secret: str = SECRET) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _worker_auth() -> dict:
    return {"Authorization": f"Bearer {_worker_token()}"}


def _create_elder(client, phone: str) -> str:
    r = client.post(
        "/v1/elders",
        json={"name": "Ada", "phone_e164": phone, "timezone": "UTC", "metadata": {}},
    )
    assert r.status_code == 201
    return r.json()["id"]


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


def test_inbound_known_elder_creates_call_and_returns_vars(client):
    phone = _phone()
    elder_id = _create_elder(client, phone)
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-1"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["elder_known"] is True
    assert data["dynamic_vars"]["elder_name"] == "Ada"
    call = client.get(f"/v1/calls/{data['call_id']}").json()
    assert call["direction"] == "inbound"
    assert call["status"] == "in_progress"
    assert call["elder_id"] == elder_id


def test_inbound_unknown_caller_creates_call_without_elder(client):
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19998887777", "livekit_room": "usan-inbound-2"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["elder_known"] is False
    assert data["dynamic_vars"] == {}
    call = client.get(f"/v1/calls/{data['call_id']}").json()
    assert call["direction"] == "inbound"
    assert call["elder_id"] is None


def test_inbound_no_phone_is_unknown(client):
    r = client.post(
        "/v1/calls/inbound",
        json={"livekit_room": "usan-inbound-3"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    assert r.json()["elder_known"] is False


def test_inbound_requires_worker_token(client):
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19998887777", "livekit_room": "usan-inbound-4"},
    )
    assert r.status_code == 401


def test_inbound_surfaces_last_check_in_and_call_id_works_with_tools(client):
    phone = _phone()
    _create_elder(client, phone)
    first = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-5a"},
        headers=_worker_auth(),
    ).json()
    call_id = first["call_id"]
    # The inbound call_id works with a call-scoped tool token (proves JWT chaining).
    w = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 4, "pain_level": 1, "notes": "a bit tired"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert w.status_code == 200
    # A later inbound call from the same elder surfaces the last check-in.
    second = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-5b"},
        headers=_worker_auth(),
    ).json()
    assert "last_check_in" in second["dynamic_vars"]
    assert "mood 4/5" in second["dynamic_vars"]["last_check_in"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_inbound.py -v`
Expected: FAIL — `POST /v1/calls/inbound` returns 404/422 (route or schema not defined).

- [ ] **Step 3a: Add the schemas**

Append to `apps/api/src/usan_api/schemas/call.py` (it already imports `Any`, `uuid`, `BaseModel`, `Field`):

```python
class InboundCallRequest(BaseModel):
    phone_e164: str | None = Field(default=None, max_length=32)
    livekit_room: str = Field(min_length=1, max_length=255)
    sip_call_id: str | None = Field(default=None, max_length=255)


class InboundCallResponse(BaseModel):
    call_id: uuid.UUID
    elder_known: bool
    dynamic_vars: dict[str, Any]
```

- [ ] **Step 3b: Add the endpoint**

In `apps/api/src/usan_api/routers/calls.py`, update imports. Change these three import lines:

```python
from usan_api.auth import require_service_token, require_worker_token
from usan_api.db.models import Call, Elder, WellnessLog
from usan_api.repositories import wellness as wellness_repo
```

(Add `require_worker_token`, `WellnessLog`, and the `wellness_repo` import alongside the existing `calls`, `dnc`, `elders` repo imports.)

Update the schema import line:

```python
from usan_api.schemas.call import (
    CallOutcomeRequest,
    CallResponse,
    CreateCallRequest,
    InboundCallRequest,
    InboundCallResponse,
)
```

Add the formatting helper near the top of the module (after `router = APIRouter(...)`):

```python
def _format_last_check_in(log: WellnessLog) -> str:
    """A short human summary of the elder's most recent wellness log."""
    parts = [f"on {log.logged_at.date().isoformat()}"]
    if log.mood is not None:
        parts.append(f"mood {log.mood}/5")
    if log.pain_level is not None:
        parts.append(f"pain {log.pain_level}/10")
    summary = ", ".join(parts)
    if log.notes:
        summary += f" — note: {log.notes}"
    return summary
```

Add the endpoint (place it right after `enqueue_call`, before `get_call`):

```python
@router.post("/inbound", response_model=InboundCallResponse)
async def register_inbound_call(
    body: InboundCallRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> InboundCallResponse:
    """Register an answered inbound call and return per-elder dynamic vars.

    Called by the agent worker once an inbound SIP caller is present (spec §3
    step 3). Looks the caller up by phone; an unknown/absent number still gets a
    call record (elder_id NULL). Never checks DNC — DNC governs outbound only.
    """
    elder = (
        await elders_repo.get_elder_by_phone(db, body.phone_e164) if body.phone_e164 else None
    )
    dynamic_vars: dict[str, Any] = {}
    if elder is not None:
        dynamic_vars["elder_name"] = elder.name
        last = await wellness_repo.get_latest_for_elder(db, elder.id)
        if last is not None:
            dynamic_vars["last_check_in"] = _format_last_check_in(last)
    call = await calls_repo.create_inbound_call(
        db,
        elder_id=elder.id if elder is not None else None,
        livekit_room=body.livekit_room,
        sip_call_id=body.sip_call_id,
        dynamic_vars=dynamic_vars,
    )
    await db.commit()
    logger.bind(call_id=str(call.id), elder_known=elder is not None).info(
        "Inbound call registered"
    )
    return InboundCallResponse(
        call_id=call.id, elder_known=elder is not None, dynamic_vars=dynamic_vars
    )
```

> Route order is safe: the only POST routes on this router are `""`, `/inbound`, and `/{call_id}/outcome`; `POST /v1/calls/inbound` matches the literal path (there is no `POST /{call_id}`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_inbound.py -v`
Expected: PASS (5 tests). Then run the full API suite: `uv run pytest -v` (all green — the change is additive).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/schemas/call.py src/usan_api/routers/calls.py tests/test_inbound.py
git commit -m "feat(api): POST /v1/calls/inbound — caller-ID lookup + inbound call record"
```

---

## Task 4: Agent — `_mint_worker_token` + `start_inbound_call`

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_api_client.py`

Work in `services/agent`.

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_api_client.py` (it already defines `SECRET`, `_settings`, and imports `jwt`, `pytest`, `api_client`):

```python
@pytest.mark.asyncio
async def test_start_inbound_call_posts_worker_token_and_returns_json(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "call_id": "inb-1",
                "elder_known": True,
                "dynamic_vars": {"elder_name": "Ada"},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    result = await api_client.start_inbound_call("+15551234567", "usan-inbound-1", _settings())

    assert result == {
        "call_id": "inb-1",
        "elder_known": True,
        "dynamic_vars": {"elder_name": "Ada"},
    }
    assert captured["url"] == "http://api:8000/v1/calls/inbound"
    assert captured["json"] == {
        "phone_e164": "+15551234567",
        "livekit_room": "usan-inbound-1",
        "sip_call_id": None,
    }
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert "call_id" not in claims  # worker token is NOT call-scoped
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_start_inbound_call_returns_none_on_error(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _BoomClient)
    result = await api_client.start_inbound_call(None, "usan-inbound-2", _settings())
    assert result is None  # best-effort: worker falls back to greet-only
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_api_client.py -v`
Expected: FAIL — `AttributeError: module 'usan_agent.api_client' has no attribute 'start_inbound_call'`.

- [ ] **Step 3: Implement `_mint_worker_token` and `start_inbound_call`**

Append to `services/agent/src/usan_agent/api_client.py` (it already imports `time`, `Any`, `cast`, `httpx`, `jwt`, `logger`, `Settings`, and defines `_TOKEN_TTL_S`):

```python
def _mint_worker_token(settings: Settings) -> str:
    """Mint a worker-scoped token (no call_id) for endpoints that create a call."""
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + _TOKEN_TTL_S},
        settings.jwt_signing_key,
        algorithm="HS256",
    )


async def start_inbound_call(
    phone_e164: str | None,
    livekit_room: str,
    settings: Settings,
    sip_call_id: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort: register an inbound call and fetch elder dynamic vars.

    Returns parsed {call_id, elder_known, dynamic_vars} on success, or None on any
    failure so the worker can fall back to a greet-only inbound conversation.
    """
    url = f"{settings.api_base_url}/v1/calls/inbound"
    headers = {"Authorization": f"Bearer {_mint_worker_token(settings)}"}
    payload = {
        "phone_e164": phone_e164,
        "livekit_room": livekit_room,
        "sip_call_id": sip_call_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
    except Exception:
        logger.bind(room=livekit_room).warning("Failed to register inbound call with API")
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_api_client.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/api_client.py tests/test_api_client.py
git commit -m "feat(agent): start_inbound_call client (worker-token inbound lookup)"
```

---

## Task 5: Agent — `build_inbound_agent`

**Files:**
- Modify: `services/agent/src/usan_agent/check_in.py`
- Test: `services/agent/tests/test_check_in.py`

Work in `services/agent`.

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_check_in.py` (it already imports `check_in`):

```python
def test_inbound_instructions_includes_name():
    text = check_in._inbound_instructions({"elder_name": "Ada"})
    assert "Ada" in text
    assert "last check-in" not in text  # no history line when absent


def test_inbound_instructions_includes_last_check_in():
    text = check_in._inbound_instructions(
        {"elder_name": "Ada", "last_check_in": "on 2026-05-30, mood 4/5"}
    )
    assert "Ada" in text
    assert "on 2026-05-30, mood 4/5" in text


def test_inbound_instructions_defaults_when_unknown():
    text = check_in._inbound_instructions({})
    assert "the caller" in text


def test_build_inbound_agent_has_same_four_tools():
    agent = check_in.build_inbound_agent({"elder_name": "Ada"})
    names = {t.id for t in agent.tools}
    assert names == {"log_wellness", "log_medication", "get_today_meds", "end_call"}
    assert "Ada" in agent.instructions
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: FAIL — `AttributeError: module 'usan_agent.check_in' has no attribute '_inbound_instructions'`.

- [ ] **Step 3: Implement the inbound agent**

Append to `services/agent/src/usan_agent/check_in.py` (it already imports `Agent`, `Any`, and defines `log_wellness`/`log_medication`/`get_today_meds`/`end_call`):

```python
INBOUND_INSTRUCTIONS_TEMPLATE = """You are a warm, patient check-in assistant from USAN Retirement,
speaking with {elder_name}, who has just called in. Speak slowly and kindly, one or two short
sentences at a time, and pause for them to answer.
{last_check_in_line}
Conduct the check-in in this order, adapting naturally to their answers:
1. Greet {elder_name} warmly by name, then ask how they are feeling today and roughly how their
   mood is. Record it with `log_wellness` (mood 1-5 where 5 is great; include any pain level 0-10
   and a short note if they mention it).
2. Use `get_today_meds` to find out which medications they take today, then gently ask whether
   they have taken each one. Record each with `log_medication`.
3. When the check-in is complete, thank them and call `end_call` with a short reason
   (for example "check_in_complete").

Never read out internal IDs or tool names. If a tool reports a problem, reassure them calmly and
continue — do not repeat a failed action more than once.
"""


def _inbound_instructions(dynamic_vars: dict[str, Any]) -> str:
    """Render the inbound instructions, weaving in the caller's dynamic vars (spec §3)."""
    elder_name = str(dynamic_vars.get("elder_name") or "the caller")
    last_check_in = dynamic_vars.get("last_check_in")
    last_check_in_line = (
        f"For context, their last check-in was {last_check_in}.\n" if last_check_in else ""
    )
    return INBOUND_INSTRUCTIONS_TEMPLATE.format(
        elder_name=elder_name, last_check_in_line=last_check_in_line
    )


def build_inbound_agent(dynamic_vars: dict[str, Any]) -> Agent:
    """The inbound check-in Agent: the four outbound tools + personalized instructions."""
    return Agent(
        instructions=_inbound_instructions(dynamic_vars),
        tools=[log_wellness, log_medication, get_today_meds, end_call],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/check_in.py tests/test_check_in.py
git commit -m "feat(agent): build_inbound_agent with personalized check-in instructions"
```

---

## Task 6: Agent — inbound worker flow

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_worker.py`

Work in `services/agent`.

- [ ] **Step 1: Replace the two inbound tests and add the caller-ID tests**

In `services/agent/tests/test_worker.py`, **delete** `test_inbound_uses_greet_only_agent` (lines ~145–174) and `test_inbound_does_not_register_transcript_flush` (lines ~210–235) — their greet-only assumption no longer holds. Add these tests at the end of the file (they reuse the existing `_settings` helper and `from unittest.mock import AsyncMock, MagicMock`):

```python
def test_caller_phone_reads_sip_attribute():
    p = MagicMock()
    p.attributes = {"sip.phoneNumber": "+15551234567"}
    assert worker._caller_phone(p) == "+15551234567"


def test_caller_phone_none_when_absent():
    p = MagicMock()
    p.attributes = {}
    assert worker._caller_phone(p) is None


async def test_inbound_known_elder_runs_check_in(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        assert phone == "+15551234567"
        assert room == "usan-inbound-x"
        return {"call_id": "inb-1", "elder_known": True, "dynamic_vars": {"elder_name": "Ada"}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)

    built = {}

    def _fake_build_inbound_agent(dynamic_vars):
        built["dynamic_vars"] = dynamic_vars
        agent = MagicMock(name="inbound_agent")
        built["agent"] = agent
        return agent

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        captured["session"] = session
        return session

    registered = {}

    def _fake_register(ctx, session, call_id, settings):
        registered["call_id"] = call_id

    fake_build_agent = MagicMock()
    monkeypatch.setattr(worker, "build_inbound_agent", _fake_build_inbound_agent)
    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "register_transcript_flush", _fake_register)
    monkeypatch.setattr(worker, "build_agent", fake_build_agent)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+15551234567"}

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert built["dynamic_vars"] == {"elder_name": "Ada"}
    assert captured["userdata"].call_id == "inb-1"
    assert captured["userdata"].job_ctx is ctx
    captured["session"].start.assert_awaited_once()
    assert captured["session"].start.await_args.kwargs["agent"] is built["agent"]
    captured["session"].generate_reply.assert_awaited_once()
    assert registered["call_id"] == "inb-1"
    fake_build_agent.assert_not_called()  # greet-only agent NOT used for a known elder


async def test_inbound_unknown_caller_falls_back_to_greet_only(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return None  # unknown caller / lookup failed

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        captured["session"] = session
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    fake_inbound_agent = MagicMock()
    monkeypatch.setattr(worker, "build_inbound_agent", fake_inbound_agent)

    flushes = {"n": 0}

    def _count_register(*a: object, **k: object) -> None:
        flushes["n"] += 1

    monkeypatch.setattr(worker, "register_transcript_flush", _count_register)

    participant = MagicMock()
    participant.attributes = {}  # no caller ID

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert captured["userdata"] is None  # greet-only carries no check-in state
    fake_inbound_agent.assert_not_called()
    assert flushes["n"] == 0
    worker.greet.assert_awaited_once()
    captured["session"].start.assert_awaited_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: FAIL — `AttributeError: module 'usan_agent.worker' has no attribute '_caller_phone'` / `start_inbound_call` / `build_inbound_agent`.

- [ ] **Step 3: Implement the inbound worker flow**

In `services/agent/src/usan_agent/worker.py`, update the two import lines:

```python
from usan_agent.api_client import start_inbound_call
from usan_agent.check_in import CheckInData, build_check_in_agent, build_inbound_agent
```

(Add `start_inbound_call` and `build_inbound_agent` to the existing imports.)

Add the constant and helpers near the top of the module (after the `parse_metadata` function, before `_run_detection_window`):

```python
_INBOUND_OPENING = (
    "Greet the caller warmly by name if you know it, and ask how they are feeling "
    "today to begin the daily check-in."
)


def _caller_phone(participant: Any) -> str | None:
    """Read the inbound caller's E.164 number from the SIP participant attributes.

    livekit-sip populates ``sip.phoneNumber`` with the remote party's number; on
    inbound that is the caller. ``sip.from`` is a fallback on newer sip servers.
    """
    attrs = getattr(participant, "attributes", None) or {}
    return attrs.get("sip.phoneNumber") or attrs.get("sip.from") or None


async def _run_inbound(ctx: JobContext, settings: Settings, log: Any) -> None:
    """Inbound: wait for the caller, look them up, run a personalized check-in.

    No voicemail detection on inbound (spec §7). A known elder gets the tool-driven
    check-in with a personalized opening + transcript flush; an unknown number or a
    failed lookup falls back to a greet-only conversation (no per-elder state, so no
    orphaned wellness/medication logs).
    """
    participant = await ctx.wait_for_participant()
    phone = _caller_phone(participant)
    log.info("Inbound caller present (phone={phone})", phone=phone)

    info = await start_inbound_call(phone, ctx.room.name, settings)
    if info and info.get("elder_known") and info.get("call_id"):
        call_id = str(info["call_id"])
        dynamic_vars = info.get("dynamic_vars") or {}
        data = CheckInData(call_id=call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_inbound_agent(dynamic_vars)
        register_transcript_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        log.info("Inbound check-in started for known elder (call_id={cid})", cid=call_id)
        await session.generate_reply(instructions=_INBOUND_OPENING)
        return

    # Unknown caller or lookup failed: greet-only, no per-elder state.
    session = build_session(settings)
    agent = build_agent()
    await session.start(agent=agent, room=ctx.room)
    log.info("Inbound greet-only (no known elder)")
    await greet(session)
```

Now restructure `entrypoint`. Replace the body from `if meta.direction == "outbound" and meta.call_id:` to the end of the function (the current lines 80–114) with:

```python
    if meta.direction == "outbound" and meta.call_id:
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_check_in_agent()
        register_transcript_flush(ctx, session, meta.call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        log.info("Session started; waiting for participant")
        try:
            await asyncio.wait_for(
                ctx.wait_for_participant(), timeout=settings.outbound_answer_timeout_s
            )
        except TimeoutError:
            # asyncio.TimeoutError is an alias of builtin TimeoutError on 3.11+.
            # The API's dial task classifies/cleans up no-answer; this is the
            # agent-side backstop so the job never hangs on an unanswered call.
            log.info("No participant within answer timeout; ending job")
            ctx.shutdown(reason="no_answer_timeout")
            return
        watcher = VoicemailWatcher()
        session.on("user_input_transcribed", lambda ev: watcher.feed(ev.transcript))
        log.info("Participant present; running voicemail detection window")
        await _run_detection_window(ctx, session, watcher, call_id=meta.call_id, settings=settings)
        return

    # Inbound: caller already dialed in; no voicemail detection (spec §7).
    await _run_inbound(ctx, settings, log)
```

> This is behavior-identical for outbound — `session.start` and the participant wait/voicemail window simply move inside the outbound branch (they were previously shared with the now-removed greet-only inbound tail). The existing `test_outbound_*` tests pass unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: PASS (existing `test_outbound_*` + `test_parse_metadata_*` + `test_run_detection_window_*` still pass; new inbound tests pass). Then `uv run pytest -v` (full agent suite green).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/worker.py tests/test_worker.py
git commit -m "feat(agent): inbound caller-ID lookup + personalized check-in"
```

---

## Task 7: Infra docs + full verification

**Files:**
- Modify: `infra/README.md`

- [ ] **Step 1: Update the inbound smoke test in `infra/README.md`**

Replace the `## Smoke test` section (currently describing the greet-only response) with:

```markdown
## Inbound flow (Plan 3d)

Inbound is now a personalized check-in. When a call arrives, the dispatch rule
spawns a metadata-less agent job (treated as inbound). The agent reads the SIP
caller-ID (`sip.phoneNumber`), POSTs it to `POST /v1/calls/inbound` (worker-token
authed), and the API looks the caller up by `phone_e164`:

- **Known elder** → an inbound `calls` row (`direction=inbound`, `in_progress`,
  `answered_at` set) is created, `dynamic_vars` (`elder_name` + last check-in) are
  returned, and the agent runs the full tool-driven check-in (wellness + medication
  logging, transcript flush) opening with a greeting by name.
- **Unknown/absent number** → a call row with `elder_id = NULL` is recorded and the
  agent gives the generic greeting only (no per-elder tools).

DNC is **not** checked on inbound (DNC governs outbound dialing only).

### Smoke test

With Telnyx pointing inbound SIP at your livekit-sip endpoint and the dispatch rule
applied (see "LiveKit side" above), and an elder whose `phone_e164` matches the
number you will call from:

```bash
# Register the elder you will call in as (E.164 must match your caller-ID)
curl -s -X POST http://localhost:8000/v1/elders -H 'content-type: application/json' \
  -d '{"name":"Test Elder","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'
```

1. Dial your Telnyx number from that phone.
2. Within ~2 seconds you should be greeted **by name** and asked how you're feeling.
3. Answer; the agent logs wellness/medications and ends the call.
4. Verify the call + transcript:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api | grep -i inbound
# find the inbound call_id in the logs, then:
curl -s http://localhost:8000/v1/calls/<CALL_ID>   # direction=inbound, status completed
```

Calling from an unknown number instead yields the generic greeting and a call row
with `elder_id: null`.
```

- [ ] **Step 2: Run BOTH full test suites + lint + type-check**

```bash
cd apps/api && uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd services/agent && uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
```
Expected: both suites fully green; ruff + mypy clean.

- [ ] **Step 3: Commit**

```bash
git add infra/README.md
git commit -m "docs(infra): document the Plan 3d inbound check-in flow"
```

---

## Self-Review

**1. Spec coverage (§3 inbound flow, §4.1 inbound webhook):**
- §3 step 2 (dispatch rule assigns agent) → already configured (`infra/livekit-sip-dispatch-rule.json`); confirmed in Context, no change.
- §3 step 3 (agent fires lookup → caller-ID → elder; dynamic vars injected) → Task 3 (`POST /v1/calls/inbound`, elder-by-phone, dynamic_vars) + Task 6 (`_caller_phone`, `start_inbound_call`, `build_inbound_agent` injecting `elder_name`/`last_check_in`). The §4.1 "POST /webhooks/livekit/room" sketch is intentionally implemented as the §3 agent-initiated model (Decision 1), documented.
- §3 step 4 (conversation loop runs, **no** voicemail detection on inbound) → Task 6 `_run_inbound` reuses the four in-call tools and never enters the voicemail window.
- Inbound call record + lifecycle (`calls.direction=inbound`, COMPLETED on room_finished, `end_call` tool) → Task 2 `create_inbound_call` (IN_PROGRESS + answered_at) + verified-existing `mark_completed_if_in_progress`/`complete_call_if_in_progress`.
- Transcript persisted on inbound → Task 6 registers `register_transcript_flush` for known elders (parity with outbound).
- Unknown-caller safety (no orphaned elder-scoped logs) → Decisions 2–3: greet-only fallback, `elder_id` NULL allowed.

**2. Placeholder scan:** Every step has complete, runnable code — auth dependency, three repo functions, two schemas + endpoint + helper, worker-token + best-effort client, inbound agent + instruction renderer, worker `_caller_phone`/`_run_inbound`/entrypoint restructure — each with verbatim tests and exact run commands. No "TBD"/"add validation"/"similar to Task N".

**3. Type/name consistency:**
- API: `get_elder_by_phone(db, phone_e164) -> Elder | None`, `create_inbound_call(db, *, elder_id, livekit_room, sip_call_id=None, dynamic_vars=None) -> Call`, `get_latest_for_elder(db, elder_id) -> WellnessLog | None` — call sites in `register_inbound_call` match exactly. `InboundCallRequest{phone_e164?, livekit_room, sip_call_id?}` / `InboundCallResponse{call_id, elder_known, dynamic_vars}` match the endpoint return and the agent's parsed dict.
- Agent: `start_inbound_call(phone_e164, livekit_room, settings, sip_call_id=None) -> dict | None` returns the keys (`call_id`, `elder_known`, `dynamic_vars`) the worker reads; `build_inbound_agent(dynamic_vars)` / `_inbound_instructions(dynamic_vars)` consume `elder_name`/`last_check_in` — exactly the keys the API emits. `_caller_phone` reads `attributes["sip.phoneNumber"]`. The worker imports `start_inbound_call` and `build_inbound_agent` as module-level names, matching the tests' `monkeypatch.setattr(worker, ...)`.
- The inbound `call_id` flows back through the agent's existing `_mint_token(call_id)` for `log_wellness`/`log_medication`/`get_today_meds`/`end_call`/`flush_transcript`, all of which already `_authorize_call` on `claims["call_id"] == str(call_id)` — verified consistent (Task 3 end-to-end test asserts this).

**Notes for the implementer:** No DB migration and no infra routing change — inbound reuses existing tables/enums and the already-applied SIP trunk + dispatch rule. The shutdown-flush and tool auth are unchanged from Plan 3a/3c. If the installed livekit-sip exposes the caller-ID under a key other than `sip.phoneNumber`/`sip.from`, adjust `_caller_phone` and note it (the design is unchanged — it just needs the caller's E.164). The brief window between participant-join and `session.start` on the known-elder path (the lookup round-trip) is acceptable because the agent greets first; if a future plan needs zero-gap audio capture, start the session before the lookup and reconfigure the agent after.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-01-plan-3d-inbound-flow.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.

**2. Inline Execution** — execute in this session using executing-plans.

**Note:** This spans both services in one worktree (Tasks 1–3 in `apps/api`, Tasks 4–6 in `services/agent`, Task 7 infra docs) — branch off the current `main` (Plans 1–3c). It completes inbound call-handling parity; remaining roadmap items are recording (§9), RAG, DTMF + reconnection (§8), observability (§11), and deployment (§14).
