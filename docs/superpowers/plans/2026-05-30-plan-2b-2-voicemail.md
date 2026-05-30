# USAN Voice Engine — Plan 2b-2: Voicemail Detection & Agent↔API Auth

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an outbound call is answered by a machine, the agent detects it from the first ~3 seconds of speech, cancels the conversation, plays a scripted leave-message, reports `voicemail_left` back to the API, and hangs up — and the agent no longer hangs forever on an unanswered call. This requires the first agent→API authentication (JWT) in the system.

**Architecture:** Voicemail detection is **agent-side and transcript-based** (Telnyx AMD is not invokable in our SIP-trunk topology — see Plan 2b-1). A pure `is_voicemail()` regex over the §7 patterns plus a `VoicemailWatcher` that accumulates STT chunks (`session.on("user_input_transcribed")`) over a 3s window. On a match the agent calls `session.interrupt(force=True)`, speaks a scripted message via `session.say(...)`, POSTs `voicemail_left` to a new JWT-authenticated `POST /v1/calls/{call_id}/outcome` endpoint, then `ctx.delete_room()` + `ctx.shutdown()`. The agent also wraps `wait_for_participant()` in a timeout so an unanswered dial ends cleanly. Service-to-service auth uses a shared `JWT_SIGNING_KEY` (HS256); the agent mints a short-lived per-call token, the API verifies it and checks the token's `call_id` matches the path.

**Tech Stack:** LiveKit Agents 1.5.14 (`AgentSession.on`/`interrupt`/`say`, `JobContext.delete_room`/`shutdown`), PyJWT (HS256), httpx (agent→API), FastAPI `HTTPBearer` dependency, pytest + testcontainers.

**Reference spec:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` (§4.2 voicemail_detector, §7 voicemail detection, §10 JWT service-to-service auth).
**Builds on:** merged Plan 2a + Plan 2b-1 (the agent worker, `dial_and_classify`, `CallStatus.VOICEMAIL_LEFT`, the calls router/repo).

---

## Research findings baked into this plan (verified against `livekit-agents 1.5.14`)

- **STT chunks:** register `session.on("user_input_transcribed", cb)`; `cb` receives a `UserInputTranscribedEvent` with `transcript: str`, `is_final: bool`, `speaker_id`, `language`, `created_at`. Fires for BOTH interim and final chunks *before* end-of-turn — exactly the sub-3s granularity needed. (`conversation_item_added` is too late.)
- **Cancel in-flight speech/LLM:** `session.interrupt(force=True)` — `force=True` cancels even the greeting (which was created `allow_interruptions=True`) and any in-flight `generate_reply`. Returns a Future.
- **Speak the leave-message:** `handle = session.say(text, allow_interruptions=False, add_to_chat_ctx=False)`; `SpeechHandle` is awaitable (`await handle`) — block until playout completes before hanging up.
- **Hang up:** `ctx.delete_room()` on the `JobContext` disconnects all participants incl. the SIP/PSTN leg (this is the actual hangup), then `ctx.shutdown(reason=...)`. This mirrors the library's own `EndCallTool`. Call `delete_room` AFTER awaiting the `say` handle or the message is cut off.
- **The apostrophe in "you've"** may be transcribed as `'`, `’`, or dropped — the regex must be permissive.
- **No agent→API client exists yet**; the agent `Settings` has no API URL or signing key. This plan adds them.
- **Version-volatile (verify at build):** the exact `UserInputTranscribedEvent` field names and whether Cartesia ink-whisper emits interim (`is_final=False`) transcripts within 3s are confirmed against the installed source but should be re-checked with a live call (`RUN_LIVE_TESTS`). Whether `JobContext.wait_for_participant` accepts a `timeout=` is unconfirmed, so this plan wraps it in `asyncio.wait_for`.

---

## File structure produced by this plan

```
apps/api/
├── pyproject.toml                      (modify: add pyjwt)
├── src/usan_api/
│   ├── settings.py                     (modify: jwt_signing_key)
│   ├── auth.py                         (create: require_service_token dependency)
│   ├── repositories/calls.py           (modify: mark_voicemail_left_if_in_progress)
│   ├── schemas/call.py                 (modify: CallOutcomeRequest)
│   └── routers/calls.py                (modify: POST /v1/calls/{call_id}/outcome)
└── tests/
    ├── conftest.py                     (modify: set JWT_SIGNING_KEY)
    ├── test_auth.py                    (create)
    └── test_calls.py                   (modify: outcome endpoint tests)

services/agent/
├── pyproject.toml                      (modify: add pyjwt)
├── src/usan_agent/
│   ├── settings.py                     (modify: api_base_url, jwt_signing_key, answer timeout)
│   ├── voicemail.py                    (create: is_voicemail + VoicemailWatcher)
│   ├── api_client.py                   (create: report_voicemail_left, JWT-signed)
│   ├── pipeline.py                     (modify: VOICEMAIL_MESSAGE constant)
│   └── worker.py                       (modify: outbound answer-timeout + voicemail branch)
└── tests/
    ├── test_voicemail.py               (create)
    ├── test_api_client.py              (create)
    └── test_worker.py                  (modify: voicemail branch helper)

infra/
├── .env.example                        (modify: JWT_SIGNING_KEY, API_BASE_URL, answer timeout)
├── docker-compose.yml                  (modify: api + agent env)
└── README.md                           (modify: voicemail + auth section)
```

**Boundary discipline:** `apps/api` and `services/agent` still do not import each other — they communicate only over HTTP (the new `/outcome` endpoint), authenticated by a shared `JWT_SIGNING_KEY`.

---

## Task 1: API — add `JWT_SIGNING_KEY` setting + pyjwt

**Files:**
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/src/usan_api/settings.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_settings.py`

`jwt_signing_key` is **required** — it gates startup (and the Alembic-on-boot entrypoint, which builds `Settings`). So `conftest.py` must set it in BOTH the Alembic-subprocess env and the `client` fixture.

- [ ] **Step 1: Add pyjwt and lock**

In `apps/api/pyproject.toml`, add to `dependencies` (after `livekit-api`):

```toml
    "pyjwt>=2.10.0",
```

Then:

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv sync
```

Expected: `pyjwt` resolved (it was already present transitively; this pins it explicitly). `uv.lock` updated.

- [ ] **Step 2: Write the failing test**

Append to `apps/api/tests/test_settings.py`:

```python
def test_jwt_signing_key_required(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)

    with pytest.raises(ValueError, match="JWT_SIGNING_KEY"):
        get_settings.cache_clear()
        get_settings()


def test_jwt_signing_key_loads(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.jwt_signing_key == "s" * 32
```

> **Note:** `get_settings` is already imported in `test_settings.py` from Plan 1; if not, add `from usan_api.settings import Settings, get_settings`.

- [ ] **Step 3: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_settings.py -k jwt -v
```

Expected: FAIL — `jwt_signing_key` field does not exist yet (and other tests that build `Settings` without it will also start failing — that is fixed in Step 5's conftest update).

- [ ] **Step 4: Add the field**

In `apps/api/src/usan_api/settings.py`, inside `Settings` (after `outbound_max_call_duration_s`):

```python
    jwt_signing_key: str = Field(..., min_length=32, alias="JWT_SIGNING_KEY")
```

- [ ] **Step 5: Set `JWT_SIGNING_KEY` in conftest (both the Alembic env and the client fixture)**

In `apps/api/tests/conftest.py`, the `database_url` fixture builds an `env` dict for the `alembic upgrade head` subprocess — add the key. Find:

```python
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": TEST_SECRET,
            "LIVEKIT_URL": "ws://livekit:7880",
        }
```

and add `"JWT_SIGNING_KEY": "s" * 32,` to that dict.

Then in the `client` fixture, find the `monkeypatch.setenv(...)` block and add:

```python
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
```

- [ ] **Step 6: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest -q
```

Expected: all PASS (the new jwt tests + the existing suite, now that conftest provides `JWT_SIGNING_KEY`).

- [ ] **Step 7: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/pyproject.toml apps/api/uv.lock apps/api/src/usan_api/settings.py apps/api/tests/conftest.py apps/api/tests/test_settings.py
git commit -m "feat(api): add required JWT_SIGNING_KEY setting + pyjwt"
```

---

## Task 2: API — service-token auth dependency

**Files:**
- Create: `apps/api/src/usan_api/auth.py`
- Create: `apps/api/tests/test_auth.py`

`require_service_token` verifies an `Authorization: Bearer <jwt>` (HS256, `JWT_SIGNING_KEY`), requires `exp` and `call_id` claims, and returns the decoded claims. Missing/invalid/expired → 401.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_auth.py`:

```python
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from usan_api.auth import require_service_token
from usan_api.settings import get_settings

SECRET = "s" * 32


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(claims: dict = Depends(require_service_token)) -> dict:
        return {"call_id": claims["call_id"]}

    return app


@pytest.fixture
def auth_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", SECRET)
    get_settings.cache_clear()
    return TestClient(_app())


def _token(secret=SECRET, *, call_id="abc", exp_delta=300, **extra) -> str:
    now = int(time.time())
    claims = {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + exp_delta}
    claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_valid_token_accepted(auth_client):
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json()["call_id"] == "abc"


def test_missing_token_401(auth_client):
    assert auth_client.get("/protected").status_code == 401


def test_wrong_secret_401(auth_client):
    bad = _token(secret="x" * 32)
    assert auth_client.get("/protected", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_expired_token_401(auth_client):
    expired = _token(exp_delta=-10)
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_missing_call_id_claim_401(auth_client):
    now = int(time.time())
    token = jwt.encode({"sub": "usan-agent", "exp": now + 300}, SECRET, algorithm="HS256")
    r = auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_auth.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.auth'`.

- [ ] **Step 3: Write `auth.py`**

Create `apps/api/src/usan_api/auth.py`:

```python
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usan_api.settings import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a service-to-service JWT (HS256). Returns the decoded claims.

    Used for agent→API calls. The token must be signed with JWT_SIGNING_KEY and
    carry `exp` and `call_id` claims. The caller is responsible for checking that
    the `call_id` claim matches the resource being mutated.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    try:
        return jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key,
            algorithms=["HS256"],
            options={"require": ["exp", "call_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token"
        ) from exc
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_auth.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/auth.py apps/api/tests/test_auth.py
git commit -m "feat(api): add require_service_token JWT dependency"
```

---

## Task 3: API — `mark_voicemail_left_if_in_progress` repository function

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Modify: `apps/api/tests/test_calls_lifecycle.py`

Mirrors `mark_completed_if_in_progress` but keyed by `call_id` and transitions to `VOICEMAIL_LEFT` (gated on `IN_PROGRESS`, records `ended_at`/`end_reason`/`duration`).

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_calls_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_mark_voicemail_left_only_when_in_progress(session_factory):
    call_id, _ = await _seed_call(session_factory, status=CallStatus.DIALING, room="vm1")
    async with session_factory() as db:
        await calls_repo.mark_answered(db, call_id, sip_call_id="SCL")
        await db.commit()
    async with session_factory() as db:
        call = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
        await db.commit()
    assert call is not None
    assert call.status is CallStatus.VOICEMAIL_LEFT
    assert call.end_reason == "voicemail"
    assert call.ended_at is not None
    assert call.duration_seconds is not None and call.duration_seconds >= 0


@pytest.mark.asyncio
async def test_mark_voicemail_left_noop_when_terminal(session_factory):
    call_id, _ = await _seed_call(session_factory, status=CallStatus.NO_ANSWER, room="vm2")
    async with session_factory() as db:
        result = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
        await db.commit()
    assert result is None
    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.NO_ANSWER
```

> **Note:** `_seed_call` (from Plan 2b-1) returns `(call_id, phone)`; the `session_factory` fixture and `CallStatus` import already exist in this file.

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls_lifecycle.py -k voicemail -v
```

Expected: FAIL — `mark_voicemail_left_if_in_progress` does not exist.

- [ ] **Step 3: Add the repository function**

In `apps/api/src/usan_api/repositories/calls.py`, add at the end of the file:

```python
async def mark_voicemail_left_if_in_progress(
    db: AsyncSession, call_id: uuid.UUID
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.VOICEMAIL_LEFT
    call.ended_at = _utcnow()
    call.end_reason = "voicemail"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls_lifecycle.py -k voicemail -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_calls_lifecycle.py
git commit -m "feat(api): add mark_voicemail_left_if_in_progress transition"
```

---

## Task 4: API — `POST /v1/calls/{call_id}/outcome` endpoint

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py`
- Modify: `apps/api/src/usan_api/routers/calls.py`
- Modify: `apps/api/tests/test_calls.py`

JWT-authenticated; the token's `call_id` claim must match the path; transitions an `in_progress` call to `voicemail_left`.

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_calls.py` (add `import time`, `import jwt` to the top of the file if not present):

```python
def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    import time

    import jwt

    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _answered_call(client, async_database_url) -> str:
    """Create a call via the API, then force it to in_progress with a direct write.

    Uses a local NullPool engine (not the production get_session_factory) so the
    write runs cleanly under asyncio.run without the cross-event-loop trap.
    """
    import asyncio
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    created = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "vm", "dynamic_vars": {}},
    )
    call_id = created.json()["id"]

    async def _answer() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                await calls_repo.mark_answered(db, _uuid.UUID(call_id), sip_call_id="SCL")
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_answer())
    return call_id


def test_outcome_marks_voicemail_left(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "voicemail_left"


def test_outcome_requires_token(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"})
    assert r.status_code == 401


def test_outcome_token_call_id_mismatch_403(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    wrong = _service_token("00000000-0000-0000-0000-000000000000")
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {wrong}"},
    )
    assert r.status_code == 403


def test_outcome_unknown_call_404(client):
    import uuid

    cid = str(uuid.uuid4())
    r = client.post(
        f"/v1/calls/{cid}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 404
```

> **Note:** the `client` fixture sets `JWT_SIGNING_KEY="s"*32` (Task 1), which is why the test signs with `"s"*32`. `_answered_call` writes `in_progress` via `get_session_factory()` — the same engine the overridden `get_db` uses, so the row is visible to the endpoint.

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -k outcome -v
```

Expected: FAIL — route not registered (404 for the wrong reason / 405).

- [ ] **Step 3: Add the request schema**

In `apps/api/src/usan_api/schemas/call.py`, add the import and the model (place `CallOutcomeRequest` after `CreateCallRequest`):

```python
from typing import Any, Literal
```

```python
class CallOutcomeRequest(BaseModel):
    outcome: Literal["voicemail_left"]
```

> **Note:** `call.py` already imports `Any` from typing; widen it to `from typing import Any, Literal`.

- [ ] **Step 4: Add the endpoint**

In `apps/api/src/usan_api/routers/calls.py`, extend the imports:

```python
from usan_api.auth import require_service_token
from usan_api.schemas.call import CallOutcomeRequest, CallResponse, CreateCallRequest
```

and add this route (after `get_call`):

```python
@router.post("/{call_id}/outcome", response_model=CallResponse)
async def report_outcome(
    call_id: uuid.UUID,
    body: CallOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(require_service_token),
) -> CallResponse:
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    # body.outcome is constrained to "voicemail_left"; gate on in_progress so a
    # late/duplicate report never overrides an already-terminal call.
    updated = await calls_repo.mark_voicemail_left_if_in_progress(db, call_id)
    await db.commit()
    logger.bind(call_id=str(call_id)).info("Call outcome reported: {o}", o=body.outcome)
    return CallResponse.from_model(updated or call)
```

> **Note:** `dict` as the `claims` annotation matches `require_service_token`'s return type and keeps mypy happy under the existing config.

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest tests/test_calls.py -k outcome -v
```

Expected: 4 PASS.

- [ ] **Step 6: Run the full API suite + lint + types**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: all green. Fix any issue before continuing.

- [ ] **Step 7: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add apps/api/src/usan_api/schemas/call.py apps/api/src/usan_api/routers/calls.py apps/api/tests/test_calls.py
git commit -m "feat(api): add JWT-authed POST /v1/calls/{id}/outcome (voicemail_left)"
```

---

## Task 5: Agent — settings for API callback + answer timeout

**Files:**
- Modify: `services/agent/pyproject.toml`
- Modify: `services/agent/src/usan_agent/settings.py`
- Modify: `services/agent/tests/test_settings.py`

- [ ] **Step 1: Add pyjwt and lock**

In `services/agent/pyproject.toml`, add to `dependencies`:

```toml
    "pyjwt>=2.10.0",
```

Then:

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv sync
```

Expected: `pyjwt` resolved; `uv.lock` updated.

- [ ] **Step 2: Write the failing test**

Append to `services/agent/tests/test_settings.py`:

```python
def test_api_callback_settings_load(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.api_base_url == "http://api:8000"
    assert s.jwt_signing_key == "s" * 32
    assert s.outbound_answer_timeout_s == 50
```

> **Note:** the existing agent `test_settings.py` builds `Settings` via env; this test adds the new required `API_BASE_URL`/`JWT_SIGNING_KEY`. Update any other test in that file that constructs `Settings()` to also set these two env vars (otherwise they will fail validation).

- [ ] **Step 3: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_settings.py -v
```

Expected: FAIL — new fields missing (and the pre-existing settings tests fail validation until they set the two new env vars — update them in Step 4).

- [ ] **Step 4: Add the fields + fix existing tests**

In `services/agent/src/usan_agent/settings.py`, inside `Settings` (after `agent_name`):

```python
    api_base_url: str = Field(..., min_length=1, alias="API_BASE_URL")
    jwt_signing_key: str = Field(..., min_length=32, alias="JWT_SIGNING_KEY")
    outbound_answer_timeout_s: float = Field(
        default=50.0, ge=5.0, le=180.0, alias="OUTBOUND_ANSWER_TIMEOUT_S"
    )
```

Then, in `services/agent/tests/test_settings.py`, add these two lines to the env setup of every existing test that constructs `Settings()` (e.g. the Plan-1 settings tests):

```python
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
```

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_settings.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/pyproject.toml services/agent/uv.lock services/agent/src/usan_agent/settings.py services/agent/tests/test_settings.py
git commit -m "feat(agent): add API_BASE_URL, JWT_SIGNING_KEY, answer-timeout settings + pyjwt"
```

---

## Task 6: Agent — voicemail classifier + watcher

**Files:**
- Create: `services/agent/src/usan_agent/voicemail.py`
- Create: `services/agent/tests/test_voicemail.py`

A pure `is_voicemail()` over the §7 regexes, and a `VoicemailWatcher` that accumulates STT chunks and exposes a detection event over a bounded window.

- [ ] **Step 1: Write the failing test**

Create `services/agent/tests/test_voicemail.py`:

```python
import asyncio

import pytest

from usan_agent.voicemail import VOICEMAIL_WINDOW_S, VoicemailWatcher, is_voicemail


@pytest.mark.parametrize(
    "text",
    [
        "Please leave a message after the tone",
        "leave a name and number",
        "You've reached the Smith residence",
        "you’ve reached us",  # curly apostrophe
        "youve reached us",  # dropped apostrophe
        "I'm not available right now",
        "please record your message after the beep",
    ],
)
def test_is_voicemail_true(text):
    assert is_voicemail(text) is True


@pytest.mark.parametrize(
    "text",
    ["Hello?", "Hi, this is Ada speaking", "who is this", ""],
)
def test_is_voicemail_false(text):
    assert is_voicemail(text) is False


def test_watcher_accumulates_across_chunks():
    w = VoicemailWatcher()
    w.feed("please")
    assert w.detected is False
    w.feed("leave a message")  # buffer now matches across chunks
    assert w.detected is True


@pytest.mark.asyncio
async def test_watcher_wait_detects_within_window():
    w = VoicemailWatcher()

    async def _later():
        await asyncio.sleep(0.01)
        w.feed("you've reached the Smiths")

    asyncio.ensure_future(_later())
    assert await w.wait_until_detected(timeout=VOICEMAIL_WINDOW_S) is True


@pytest.mark.asyncio
async def test_watcher_wait_times_out_for_human():
    w = VoicemailWatcher()
    w.feed("hello who is this")
    assert await w.wait_until_detected(timeout=0.05) is False
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_voicemail.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.voicemail'`.

- [ ] **Step 3: Write `voicemail.py`**

Create `services/agent/src/usan_agent/voicemail.py`:

```python
"""Transcript-based voicemail detection (design spec §7).

Telnyx AMD is not invokable in our LiveKit-SIP-trunk topology, so voicemail is
detected agent-side from the first few seconds of STT. `is_voicemail` is a pure
classifier; `VoicemailWatcher` accumulates interim+final STT chunks and signals
once a voicemail greeting is recognised within the detection window.
"""

import asyncio
import re

# Seconds after the callee answers to listen for a voicemail greeting.
VOICEMAIL_WINDOW_S = 3.0

# §7 patterns, case-insensitive. The apostrophe in "you've" may be transcribed
# as ', ’, or dropped, so it is optional.
_PATTERN = re.compile(
    r"leave a (?:message|name)"
    r"|you[’']?ve reached"
    r"|not available right now"
    r"|after the (?:beep|tone)",
    re.IGNORECASE,
)


def is_voicemail(text: str) -> bool:
    return bool(_PATTERN.search(text))


class VoicemailWatcher:
    """Accumulate STT chunks and flag when a voicemail greeting is recognised."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event = asyncio.Event()

    def feed(self, transcript: str) -> None:
        # Interim chunks are revised rather than strictly additive, but matching
        # the §7 phrases against the running buffer is robust to that.
        self._buffer = f"{self._buffer} {transcript}".strip()
        if is_voicemail(self._buffer):
            self._event.set()

    @property
    def detected(self) -> bool:
        return self._event.is_set()

    async def wait_until_detected(self, timeout: float) -> bool:
        """True if a voicemail greeting is detected within `timeout` seconds."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_voicemail.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/src/usan_agent/voicemail.py services/agent/tests/test_voicemail.py
git commit -m "feat(agent): add transcript voicemail classifier + watcher"
```

---

## Task 7: Agent — JWT-signed API client

**Files:**
- Create: `services/agent/src/usan_agent/api_client.py`
- Create: `services/agent/tests/test_api_client.py`

`report_voicemail_left` mints a short-lived per-call JWT (HS256, `JWT_SIGNING_KEY`) and POSTs `{"outcome": "voicemail_left"}` to the API. Best-effort: it logs and swallows errors so a failed report never strands the SIP leg.

- [ ] **Step 1: Write the failing test**

Create `services/agent/tests/test_api_client.py`:

```python
import jwt
import pytest

from usan_agent import api_client
from usan_agent.settings import Settings

SECRET = "s" * 32


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="cart",
        GEMINI_API_KEY="gem",
        DEFAULT_CARTESIA_VOICE_ID="voice",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY=SECRET,
    )


@pytest.mark.asyncio
async def test_report_voicemail_left_posts_signed_request(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

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

    await api_client.report_voicemail_left("call-123", _settings())

    assert captured["url"] == "http://api:8000/v1/calls/call-123/outcome"
    assert captured["json"] == {"outcome": "voicemail_left"}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["call_id"] == "call-123"
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_report_voicemail_left_swallows_errors(monkeypatch):
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
    # must NOT raise — the hangup proceeds regardless
    await api_client.report_voicemail_left("call-456", _settings())
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_api_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.api_client'`.

- [ ] **Step 3: Write `api_client.py`**

Create `services/agent/src/usan_agent/api_client.py`:

```python
"""Thin JWT-authenticated HTTP client for agent→API calls (design spec §10).

The agent and API share JWT_SIGNING_KEY; the agent mints a short-lived per-call
token so the API can both authenticate the agent and confirm the token is scoped
to the call being mutated.
"""

import time

import httpx
import jwt
from loguru import logger

from usan_agent.settings import Settings

_TOKEN_TTL_S = 300


def _mint_token(call_id: str, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + _TOKEN_TTL_S},
        settings.jwt_signing_key,
        algorithm="HS256",
    )


async def report_voicemail_left(call_id: str, settings: Settings) -> None:
    """Best-effort report that a call reached voicemail. Never raises."""
    url = f"{settings.api_base_url}/v1/calls/{call_id}/outcome"
    headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"outcome": "voicemail_left"}, headers=headers
            )
            response.raise_for_status()
        logger.bind(call_id=call_id).info("Reported voicemail_left to API")
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to report voicemail_left to API")
```

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_api_client.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client.py
git commit -m "feat(agent): add JWT-signed API client (report_voicemail_left)"
```

---

## Task 8: Agent — voicemail leave-message constant + handler

**Files:**
- Modify: `services/agent/src/usan_agent/pipeline.py`
- Create: `services/agent/src/usan_agent/voicemail_action.py`
- Create: `services/agent/tests/test_voicemail_action.py`

The hangup sequence (interrupt → say → report → delete_room → shutdown) is extracted into a testable async helper so the worker entrypoint stays thin and the branch is unit-tested with mocks.

- [ ] **Step 1: Add the leave-message constant**

In `services/agent/src/usan_agent/pipeline.py`, after the `GREETING` constant, add:

```python
VOICEMAIL_MESSAGE = (
    "Hello, this is your daily check-in from USAN Retirement. "
    "We're sorry we missed you. We'll try again a little later. "
    "Take care, and have a wonderful day."
)
```

- [ ] **Step 2: Write the failing test**

Create `services/agent/tests/test_voicemail_action.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import voicemail_action
from usan_agent.pipeline import VOICEMAIL_MESSAGE


def _settings() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_leave_voicemail_sequence(monkeypatch):
    reported = []

    async def _fake_report(call_id, settings):
        reported.append(call_id)

    monkeypatch.setattr(voicemail_action, "report_voicemail_left", _fake_report)

    ctx = MagicMock()
    ctx.delete_room = AsyncMock()
    ctx.shutdown = MagicMock()

    say_handle = AsyncMock()
    session = MagicMock()
    session.interrupt = MagicMock()
    session.say = MagicMock(return_value=say_handle)

    await voicemail_action.leave_voicemail(ctx, session, "call-789", _settings())

    session.interrupt.assert_called_once_with(force=True)
    session.say.assert_called_once()
    assert session.say.call_args.args[0] == VOICEMAIL_MESSAGE
    assert session.say.call_args.kwargs["allow_interruptions"] is False
    say_handle.assert_awaited()  # waited for playout before hanging up
    assert reported == ["call-789"]
    ctx.delete_room.assert_awaited_once()
    ctx.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_leave_voicemail_skips_report_without_call_id(monkeypatch):
    called = []
    monkeypatch.setattr(
        voicemail_action,
        "report_voicemail_left",
        AsyncMock(side_effect=lambda *a: called.append(a)),
    )
    ctx = MagicMock()
    ctx.delete_room = AsyncMock()
    ctx.shutdown = MagicMock()
    session = MagicMock()
    session.interrupt = MagicMock()
    session.say = MagicMock(return_value=AsyncMock())

    await voicemail_action.leave_voicemail(ctx, session, None, _settings())

    assert called == []  # no call_id → nothing to report
    ctx.delete_room.assert_awaited_once()
```

> **Note:** `session.interrupt(force=True)` returns a Future in production; this helper does not await it (the subsequent `say` supersedes any in-flight speech). If a future livekit-agents version requires awaiting the interrupt, adjust both the helper and this assertion.

- [ ] **Step 3: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_voicemail_action.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.voicemail_action'`.

- [ ] **Step 4: Write `voicemail_action.py`**

Create `services/agent/src/usan_agent/voicemail_action.py`:

```python
"""The voicemail hangup sequence, extracted for unit testing.

cancel in-flight speech/LLM → speak the scripted leave-message → report the
outcome to the API → delete the room (hangs up the SIP leg) → shut the job down.
"""

from typing import Any

from loguru import logger

from usan_agent.api_client import report_voicemail_left
from usan_agent.pipeline import VOICEMAIL_MESSAGE
from usan_agent.settings import Settings


async def leave_voicemail(
    ctx: Any, session: Any, call_id: str | None, settings: Settings
) -> None:
    log = logger.bind(call_id=call_id)
    log.info("Voicemail detected; leaving scripted message")

    session.interrupt(force=True)  # cancel the greeting / any in-flight reply
    handle = session.say(
        VOICEMAIL_MESSAGE, allow_interruptions=False, add_to_chat_ctx=False
    )
    await handle  # wait for full playout before hanging up

    if call_id:
        await report_voicemail_left(call_id, settings)

    await ctx.delete_room()  # disconnects the SIP/PSTN leg = hangup
    ctx.shutdown(reason="voicemail_left")
```

- [ ] **Step 5: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_voicemail_action.py -v
```

Expected: 2 PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/src/usan_agent/pipeline.py services/agent/src/usan_agent/voicemail_action.py services/agent/tests/test_voicemail_action.py
git commit -m "feat(agent): add voicemail leave-message + hangup handler"
```

---

## Task 9: Agent — wire detection + answer-timeout into the worker

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Modify: `services/agent/tests/test_worker.py`

The entrypoint now: for outbound, wrap `wait_for_participant()` in a timeout (clean exit on no-answer); after answer, attach the voicemail watcher, greet, and over the detection window either leave a voicemail or fall through to the (Plan 1) conversation. Inbound is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `services/agent/tests/test_worker.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker
from usan_agent.voicemail import VoicemailWatcher


@pytest.mark.asyncio
async def test_run_detection_window_triggers_voicemail(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)

    watcher = VoicemailWatcher()
    watcher.feed("you've reached the Smiths, leave a message")  # already detected

    session = MagicMock()
    ctx = MagicMock()
    greeted = []

    async def _greet(_s):
        greeted.append(True)

    monkeypatch.setattr(worker, "greet", _greet)

    await worker._run_detection_window(
        ctx, session, watcher, call_id="c1", settings=MagicMock()
    )

    assert greeted == [True]
    assert left == ["c1"]


@pytest.mark.asyncio
async def test_run_detection_window_human_falls_through(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    # shorten the window so the test is fast
    monkeypatch.setattr(worker, "VOICEMAIL_WINDOW_S", 0.05)

    watcher = VoicemailWatcher()  # never fed a voicemail phrase

    await worker._run_detection_window(
        MagicMock(), MagicMock(), watcher, call_id="c2", settings=MagicMock()
    )

    assert left == []  # human → no voicemail action
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest tests/test_worker.py -k detection_window -v
```

Expected: FAIL — `worker._run_detection_window` does not exist.

- [ ] **Step 3: Rewrite `worker.py`**

Replace the entire contents of `services/agent/src/usan_agent/worker.py` with:

```python
"""LiveKit Agents 1.x worker entrypoint.

Run with:
    uv run python -m usan_agent.worker dev    # development mode
    uv run python -m usan_agent.worker start  # production mode
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from usan_agent.logging_config import configure_logging
from usan_agent.pipeline import build_agent, build_session, greet
from usan_agent.settings import Settings, get_settings
from usan_agent.voicemail import VOICEMAIL_WINDOW_S, VoicemailWatcher
from usan_agent.voicemail_action import leave_voicemail


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


async def _run_detection_window(
    ctx: JobContext,
    session: Any,
    watcher: VoicemailWatcher,
    *,
    call_id: str | None,
    settings: Settings,
) -> None:
    """Greet, then over the detection window leave a voicemail or fall through."""
    await greet(session)
    if await watcher.wait_until_detected(timeout=VOICEMAIL_WINDOW_S):
        await leave_voicemail(ctx, session, call_id, settings)
    # else: a human answered — the conversation continues (single-turn in Plan 1).


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

    if meta.direction == "outbound":
        try:
            await asyncio.wait_for(
                ctx.wait_for_participant(), timeout=settings.outbound_answer_timeout_s
            )
        except (TimeoutError, asyncio.TimeoutError):
            # The API's dial task classifies/cleans up no-answer; this is the
            # agent-side backstop so the job never hangs on an unanswered call.
            log.info("No participant within answer timeout; ending job")
            ctx.shutdown(reason="no_answer_timeout")
            return

        watcher = VoicemailWatcher()
        session.on("user_input_transcribed", lambda ev: watcher.feed(ev.transcript))
        log.info("Participant present; running voicemail detection window")
        await _run_detection_window(
            ctx, session, watcher, call_id=meta.call_id, settings=settings
        )
        return

    # Inbound: caller already present; no voicemail detection (spec §7).
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

> **Note for the engineer:** the lambda passed to `session.on("user_input_transcribed", ...)` reads `ev.transcript`. Confirm that attribute name against the installed `livekit-agents` (`UserInputTranscribedEvent.transcript`). If Cartesia ink-whisper does not emit interim transcripts, the 3s window may only see final chunks — verify with a live voicemail fixture (`RUN_LIVE_TESTS=1`). `session.on` is a synchronous registration that returns the callback; we leave it attached (a late match after the window has no awaiter and is harmless).

- [ ] **Step 4: Run to verify pass**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run pytest -q
```

Expected: all PASS (the existing `parse_metadata` tests + the new detection-window tests).

- [ ] **Step 5: Lint + types**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: clean. Fix any issues before continuing.

- [ ] **Step 6: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add services/agent/src/usan_agent/worker.py services/agent/tests/test_worker.py
git commit -m "feat(agent): voicemail detection window + outbound answer-timeout in worker"
```

---

## Task 10: Infra — JWT key + API base URL wiring

**Files:**
- Modify: `infra/.env.example`
- Modify: `infra/docker-compose.yml`
- Modify: `infra/README.md`

- [ ] **Step 1: Extend `infra/.env.example`**

Append to `infra/.env.example`:

```bash

# === Service-to-service auth (agent -> api) ===
# Shared HS256 key the agent signs callbacks with and the API verifies.
# Generate with: openssl rand -hex 32
JWT_SIGNING_KEY=change-me-with-openssl-rand-hex-32
# Base URL the agent uses to reach the API (compose network service name).
API_BASE_URL=http://api:8000
# Agent-side backstop: seconds to wait for the callee to answer before ending
# the job (the API's dial task is the primary no-answer path).
# OUTBOUND_ANSWER_TIMEOUT_S=50
```

- [ ] **Step 2: Wire env into compose**

In `infra/docker-compose.yml`, in the `api` service `environment:` block, add:

```yaml
      JWT_SIGNING_KEY: ${JWT_SIGNING_KEY}
```

and in the `agent` service `environment:` block, add:

```yaml
      JWT_SIGNING_KEY: ${JWT_SIGNING_KEY}
      API_BASE_URL: ${API_BASE_URL}
```

- [ ] **Step 3: Document in `infra/README.md`**

Append to `infra/README.md`:

````markdown
## Voicemail detection & agent→API auth (Plan 2b-2)

On an answered outbound call the agent listens to the first ~3s of speech. If it
matches a voicemail greeting (e.g. "leave a message", "you've reached", "after the
beep"), the agent cancels the conversation, plays a scripted leave-message, reports
`voicemail_left` to the API, and hangs up (`delete_room`). An unanswered outbound
call ends cleanly via an agent-side answer timeout.

The agent→API report (`POST /v1/calls/{id}/outcome`) is authenticated with a
short-lived HS256 JWT signed with the shared `JWT_SIGNING_KEY`; the API verifies
the signature and that the token's `call_id` matches the path. Set a strong
`JWT_SIGNING_KEY` (`openssl rand -hex 32`) in `infra/.env` — it is required by
both `api` and `agent` at startup.

> Telnyx AMD is intentionally NOT used (our SIP-trunk topology can't invoke it).
> Retry of `voicemail_left` (one attempt after 3h) and TCPA quiet hours are Plan 2b-3.
````

- [ ] **Step 4: Validate compose**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
docker compose --env-file infra/.env.example -f infra/docker-compose.yml config >/dev/null && echo "compose ok"
```

Expected: `compose ok` (the `.env.example` now provides `JWT_SIGNING_KEY` and `API_BASE_URL`).

- [ ] **Step 5: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add infra/.env.example infra/docker-compose.yml infra/README.md
git commit -m "feat(infra): wire JWT_SIGNING_KEY + API_BASE_URL for agent->api auth"
```

---

## Task 11: Verification, stack bring-up, and PR

- [ ] **Step 1: Full suites + lint + types (both packages)**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api
uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd ../../services/agent
uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: all green in both packages.

- [ ] **Step 2: Pre-commit on all files**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
pre-commit run --all-files
```

Expected: clean. Stage + commit any auto-fixes.

- [ ] **Step 3: Stack bring-up sanity**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
# ensure infra/.env has a real JWT_SIGNING_KEY (openssl rand -hex 32) and API_BASE_URL
make up
docker compose --env-file infra/.env -f infra/docker-compose.yml ps
curl -sf http://localhost:8000/health
docker compose --env-file infra/.env -f infra/docker-compose.yml logs agent | grep -i "agent_name\|registered" | tail -5
```

Expected: services healthy; `/health` ok; agent registered. (The API now requires `JWT_SIGNING_KEY` at boot — if it crash-loops, that env var is missing.)

- [ ] **Step 4: Live voicemail smoke (mandatory if outbound is configured)**

With outbound configured (Plan 2a/2b-1) and `JWT_SIGNING_KEY` set, place a call to a number that goes to voicemail. Confirm: the agent plays the leave-message, then `GET /v1/calls/{id}` shows `voicemail_left` with `ended_at`/`duration_seconds`. **Verify the STT actually surfaces the voicemail greeting within 3s** — if calls reach voicemail but stay `in_progress` (detection missed), inspect the agent logs for `user_input_transcribed` events and adjust `VOICEMAIL_WINDOW_S` or the patterns. Also place a call that is not answered and confirm the agent job ends (answer timeout) and the call is `no_answer`. Record results in `infra/README.md` under "Outbound smoke test result"; document any deferral if no public IP.

- [ ] **Step 5: Open the PR**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git checkout -b feat/plan-2b-2-voicemail
git push -u origin feat/plan-2b-2-voicemail
gh pr create --title "feat: Plan 2b-2 — voicemail detection & agent↔API auth" --body "$(cat <<'EOF'
## Summary
- Agent-side transcript voicemail detection (§7 regexes) over the first ~3s of STT; on match: interrupt → scripted leave-message → report → hangup.
- First agent→API authentication: shared HS256 JWT_SIGNING_KEY; agent mints a short-lived per-call token, API verifies it and checks the call_id claim matches the path.
- New JWT-authed POST /v1/calls/{id}/outcome → voicemail_left (gated on in_progress).
- Agent-side answer timeout so an unanswered outbound call ends cleanly.

Telnyx AMD remains deferred (not invokable in our SIP-trunk topology). Retry of voicemail_left + TCPA quiet hours are Plan 2b-3.

## Test plan
- [ ] apps/api: pytest (testcontainers), ruff, mypy green; auth + outcome-endpoint tests
- [ ] services/agent: pytest, ruff, mypy green; voicemail classifier/watcher, api_client, voicemail_action, detection-window tests
- [ ] Stack boots with JWT_SIGNING_KEY; /health ok; agent registered
- [ ] Live: a voicemail call → voicemail_left; an unanswered call → no_answer + agent exits (or documented deferral)
EOF
)"
```

Expected: PR created; CI green.

---

## Plan 2b-2 done criteria

1. `uv run pytest`/`ruff`/`mypy` green in both `apps/api` and `services/agent`.
2. `POST /v1/calls/{id}/outcome` requires a valid service JWT (401 missing/invalid/expired, 403 on call_id mismatch), 404 for unknown call, and transitions an `in_progress` call to `voicemail_left` (no-op otherwise).
3. The agent detects a voicemail greeting within the window, plays the leave-message, reports `voicemail_left`, and hangs up; a human answer falls through to the conversation; an unanswered call ends via the answer timeout.
4. `JWT_SIGNING_KEY` is required by both services and wired through compose; the stack boots.
5. CI green on the pushed branch; live voicemail smoke validated or deferral documented.

## What's NOT in Plan 2b-2 (next)

- **Plan 2b-3:** retry orchestrator (DB `FOR UPDATE SKIP LOCKED` poller), the §5.3 retry policy (incl. `voicemail_left` → one retry after 3h), `parent_call_id`/`attempt` chaining, TCPA quiet hours (`zoneinfo` + `tzdata`), DNC re-check at retry, and extending `idx_calls_status_scheduled` to cover `busy`/`failed`.
- **Deferred:** Telnyx AMD (only if outbound origination moves to the Telnyx Voice API); per-elder voicemail message; multi-turn conversation (still Plan 1 single-turn); the agent graceful-teardown when the API deletes the room out from under it (the answer-timeout + the room-deleted disconnect cover the common cases).
