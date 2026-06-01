# Plan 3b — Agent Conversation Loop & In-Call Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the outbound agent actually conduct the wellness check-in — a guided multi-turn conversation whose LLM calls `@function_tool`s that hit the Plan 3a `/v1/tools/*` endpoints (`get_today_meds`, `log_wellness`, `log_medication`, `end_call`), ending the call gracefully.

**Architecture:** The STT→LLM→TTS turn loop already runs automatically once `session.start()` is called (livekit-agents 1.5.14 blocks the job on `_shutdown_fut` after the entrypoint returns). So this plan adds three things: (1) agent-side HTTP client functions for the four tool endpoints (reusing the per-call JWT from `api_client.py`); (2) a `CheckInAgent`/tool set with a structured check-in system prompt, where each tool reads the `call_id` + `settings` + `JobContext` from the session's typed `userdata` (`RunContext.userdata`); (3) wiring so an **outbound** call starts the session with that agent + userdata. Inbound stays greet-only (no `call_id`, out of scope). `end_call` says goodbye then hangs up via the captured `JobContext` (mirroring the existing `leave_voicemail`).

**Tech Stack:** livekit-agents 1.5.14 (`function_tool`, `RunContext`, `Agent(tools=...)`, `AgentSession[T](userdata=...)`), livekit-plugins-google (Gemini), httpx, PyJWT (HS256), pytest (`asyncio_mode=auto`).

---

## Context for the implementer

Work in `services/agent` (Python 3.12, `uv`). Run from `services/agent/`:

```bash
cd services/agent && uv sync
uv run pytest -v
uv run ruff check . && uv run ruff format .
uv run mypy
```

Conventions (match the existing service):
- **ruff** `select = ["E","F","I","B","UP","ASYNC","S","PT","RET","SIM"]`, line-length 100, target py312, `S` ignored under `tests/**`, `S101` globally ignored. No `try/except/pass` (S110) — log or `contextlib.suppress`; no `async def` param named `timeout` (ASYNC109); single-statement `pytest.raises` body (PT012).
- **mypy** `--strict` on `src` only, `plugins=["pydantic.mypy"]`.
- **Tests**: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed), no conftest — mock inline with `monkeypatch` + `unittest.mock` (`MagicMock` for sync, `AsyncMock` for awaited).
- Commit `type(scope): description`, scope `agent`. **No `Co-Authored-By` trailer.**
- `services/agent` must NOT import from `apps/api` — it calls the API over HTTP.

### Current agent code (verified — you will extend these)

- `src/usan_agent/api_client.py` — `_mint_token(call_id, settings) -> str` (HS256, claims `sub/call_id/iat/exp`, `_TOKEN_TTL_S=300`), `report_voicemail_left(call_id, settings)` (best-effort POST to `/v1/calls/{id}/outcome`, swallows errors). Imports `time`, `httpx`, `jwt`, `logger`, `Settings`.
- `src/usan_agent/pipeline.py` — `SYSTEM_PROMPT`, `GREETING`, `VOICEMAIL_MESSAGE`, `STT_MODEL`, `LLM_MODEL`; `build_session(settings) -> AgentSession[None]` (VAD/STT/LLM/TTS/turn_detection; **no `userdata`, no `tools`**); `build_agent() -> Agent` (`Agent(instructions=SYSTEM_PROMPT, chat_ctx=ChatContext())`, no tools); `greet(session)` (`await session.say(GREETING, allow_interruptions=True)`).
- `src/usan_agent/worker.py` — `entrypoint(ctx)`: `await ctx.connect()`; `session = build_session(settings)`; `agent = build_agent()`; `await session.start(agent=agent, room=ctx.room)`; then **outbound** branch (`asyncio.wait_for(ctx.wait_for_participant(), timeout=settings.outbound_answer_timeout_s)`; on timeout `ctx.shutdown(reason="no_answer_timeout")`; else `VoicemailWatcher` subscribed to `session.on("user_input_transcribed", ...)`; `_run_detection_window(ctx, session, watcher, call_id=meta.call_id, settings=settings)`); **inbound** branch (`await ctx.wait_for_participant()`; `await greet(session)`). `parse_metadata(raw) -> CallMetadata(call_id, direction, dynamic_vars)`.
- `src/usan_agent/voicemail_action.py` — `leave_voicemail(ctx, session, call_id, settings)`: `session.interrupt(force=True)` → `handle = session.say(VOICEMAIL_MESSAGE, allow_interruptions=False, add_to_chat_ctx=False)` → `await handle` → `if call_id: await report_voicemail_left(call_id, settings)` → `await ctx.delete_room()` → `ctx.shutdown(reason="voicemail_left")`. **This is the hangup pattern `end_call` mirrors.**
- `src/usan_agent/settings.py` — `Settings`: `api_base_url` (API_BASE_URL, required), `jwt_signing_key` (JWT_SIGNING_KEY, min 32), `outbound_answer_timeout_s` (float, default 50), plus livekit/cartesia/gemini/voice/agent_name/log_level.
- `tests/` — `test_voicemail.py`, `test_voicemail_action.py`: mock `session = MagicMock()` with `session.say = AsyncMock()`, `session.interrupt = MagicMock()`, `ctx = MagicMock()` with `ctx.delete_room = AsyncMock()`, `ctx.shutdown = MagicMock()`; `monkeypatch.setattr(...)` for module-level functions.

### Installed livekit-agents 1.5.14 API (verified via introspection)

- **Tools:** `from livekit.agents import function_tool, RunContext`. A tool is `@function_tool async def name(ctx: RunContext, <params>) -> str: ...`; the docstring becomes the LLM description; the first param after `ctx` onward are the LLM-visible args. Decorated, `name` becomes a `FunctionTool` (has `.name`/`.id`).
- **Attach:** `Agent(instructions=..., tools=[t1, t2, ...])` accepts a `tools` list of `FunctionTool`s.
- **Per-call state:** `AgentSession[T](userdata=T(...))`; inside a tool, `ctx.userdata` returns that `T` (raises `ValueError` if the session had no userdata). We use `T = CheckInData`.
- **Hang up from a tool:** `RunContext` exposes `.session`, `.userdata`, `.function_call`, `.speech_handle` — **not** the `JobContext`. So the tool reaches the `JobContext` through `ctx.userdata.job_ctx` (we store it). `ctx.session.say(...)` returns a `SpeechHandle` that is awaitable; `job_ctx.delete_room()` returns an awaitable `Task`; `job_ctx.shutdown(reason=...)` is sync. (Mirror `leave_voicemail`.)
- **`AgentSession.say(text, allow_interruptions=..., add_to_chat_ctx=...)`** returns an awaitable `SpeechHandle`.

### Decisions locked (do not silently change)

1. **Outbound only.** The check-in conversation + tools apply to **outbound** calls with a `call_id`. Inbound keeps the current greet-only `build_agent()` (no `call_id`, no tools) — inbound conversation is a future plan.
2. **Per-call state via `userdata`.** The session carries a `CheckInData(call_id, settings, job_ctx)`. Tools read `ctx.userdata`. This keeps tools as standalone testable functions and avoids globals.
3. **Tool logic in plain helpers.** Each `@function_tool` is a thin wrapper over a plain `_do_*` async helper (testable directly with a `CheckInData` + mocked `api_client`). The decorator wrapping is not unit-invoked.
4. **Client functions raise; tool wrappers catch.** `api_client` tool functions call `raise_for_status()`; the `_do_*` helpers catch exceptions and return a calm, elder-appropriate string so a transient API failure never crashes the call.
5. **`end_call` mirrors `leave_voicemail`:** best-effort report the reason to the API → say goodbye → `job_ctx.delete_room()` → `job_ctx.shutdown(reason="ended_by_agent")`.
6. **Greeting unchanged.** `greet()` still speaks `GREETING` (it also drives the 3s voicemail window). After it, the `CheckInAgent`'s LLM continues the structured check-in.

---

## File Structure

**Create:**
- `src/usan_agent/check_in.py` — `CheckInData`, `CHECK_IN_INSTRUCTIONS`, `GOODBYE_MESSAGE`, the `_do_*` helpers, the four `@function_tool`s, and `build_check_in_agent()`.
- `tests/test_api_client_tools.py` — tests for the four `api_client` tool functions.
- `tests/test_check_in.py` — tests for the `_do_*` helpers + `build_check_in_agent`.

**Modify:**
- `src/usan_agent/api_client.py` — add `_post_tool` + `log_wellness` / `log_medication` / `get_today_meds` / `report_end_call`.
- `src/usan_agent/pipeline.py` — `build_session` accepts optional `userdata`.
- `src/usan_agent/worker.py` — outbound starts the session with `CheckInData` + `build_check_in_agent()`.
- `tests/test_worker.py` (or the existing worker test module) — outbound wiring test.

---

## Task 1: Agent-side tool client functions

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_api_client_tools.py`

Add HTTP clients for the four tool endpoints. They reuse `_mint_token`, POST `{call_id, ...}` with the bearer token, and `raise_for_status()` (callers handle failures).

- [ ] **Step 1: Write the failing tests**

Create `services/agent/tests/test_api_client_tools.py`:

```python
import httpx
import pytest

from usan_agent import api_client
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


class _FakeClient:
    """Stands in for httpx.AsyncClient; records the request and returns a canned response."""

    captured: dict = {}
    status = 200
    json_data: dict = {}

    def __init__(self, timeout=None):
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        request = httpx.Request("POST", url)
        return httpx.Response(_FakeClient.status, json=_FakeClient.json_data, request=request)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 200
    _FakeClient.json_data = {}
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def test_log_wellness_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 7}
    await api_client.log_wellness("call-1", _settings(), mood=4, pain_level=2, notes="ok")
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/log_wellness"
    assert cap["json"] == {"call_id": "call-1", "mood": 4, "pain_level": 2, "notes": "ok"}
    assert cap["headers"]["Authorization"].startswith("Bearer ")


async def test_log_medication_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 9}
    await api_client.log_medication(
        "call-1", _settings(), medication_name="Aspirin", taken=True, reported_time=None
    )
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/log_medication"
    assert cap["json"] == {
        "call_id": "call-1",
        "medication_name": "Aspirin",
        "taken": True,
        "reported_time": None,
    }


async def test_get_today_meds_returns_medications(fake_http):
    _FakeClient.json_data = {"medications": [{"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}]}
    meds = await api_client.get_today_meds("call-1", _settings())
    assert meds == [{"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}]
    assert fake_http.captured["json"] == {"call_id": "call-1"}


async def test_report_end_call_posts_reason(fake_http):
    _FakeClient.json_data = {"status": "completed"}
    await api_client.report_end_call("call-1", _settings(), "check_in_complete")
    assert fake_http.captured["url"] == "http://api:8000/v1/tools/end_call"
    assert fake_http.captured["json"] == {"call_id": "call-1", "reason": "check_in_complete"}


async def test_tool_client_raises_on_http_error(fake_http):
    _FakeClient.status = 500
    with pytest.raises(httpx.HTTPStatusError):
        await api_client.log_wellness("call-1", _settings(), mood=3, pain_level=None, notes=None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_api_client_tools.py -v`
Expected: FAIL — `AttributeError: module 'usan_agent.api_client' has no attribute 'log_wellness'`.

- [ ] **Step 3: Add the client functions**

In `services/agent/src/usan_agent/api_client.py`, add `from typing import Any` to the imports (after `import time`), then append:

```python
async def _post_tool(
    tool: str, call_id: str, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST a JWT-scoped tool request to the API and return the parsed JSON.

    Raises httpx.HTTPStatusError on a non-2xx response; callers decide how to
    surface that to the conversation.
    """
    url = f"{settings.api_base_url}/v1/tools/{tool}"
    headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json={"call_id": call_id, **payload}, headers=headers)
        response.raise_for_status()
        return response.json()


async def log_wellness(
    call_id: str,
    settings: Settings,
    *,
    mood: int | None,
    pain_level: int | None,
    notes: str | None,
) -> None:
    await _post_tool(
        "log_wellness",
        call_id,
        settings,
        {"mood": mood, "pain_level": pain_level, "notes": notes},
    )


async def log_medication(
    call_id: str,
    settings: Settings,
    *,
    medication_name: str,
    taken: bool,
    reported_time: str | None = None,
) -> None:
    await _post_tool(
        "log_medication",
        call_id,
        settings,
        {"medication_name": medication_name, "taken": taken, "reported_time": reported_time},
    )


async def get_today_meds(call_id: str, settings: Settings) -> list[dict[str, Any]]:
    data = await _post_tool("get_today_meds", call_id, settings, {})
    meds = data.get("medications", [])
    return meds if isinstance(meds, list) else []


async def report_end_call(call_id: str, settings: Settings, reason: str) -> None:
    await _post_tool("end_call", call_id, settings, {"reason": reason})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_api_client_tools.py -v`
Expected: PASS (5 cases). Confirm no regression: `uv run pytest -v`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/api_client.py tests/test_api_client_tools.py
git commit -m "feat(agent): add in-call tool HTTP client functions"
```

---

## Task 2: Check-in tools + agent builder

**Files:**
- Create: `services/agent/src/usan_agent/check_in.py`
- Test: `services/agent/tests/test_check_in.py`

`CheckInData` carries per-call state; each `@function_tool` reads `ctx.userdata` and delegates to a plain, testable `_do_*` helper that catches API errors and returns an elder-appropriate string. `end_call` says goodbye and hangs up via the captured `JobContext`.

- [ ] **Step 1: Write the failing tests**

Create `services/agent/tests/test_check_in.py`:

```python
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import check_in
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


def _data(job_ctx=None) -> check_in.CheckInData:
    return check_in.CheckInData(call_id="call-1", settings=_settings(), job_ctx=job_ctx or MagicMock())


async def test_do_log_wellness_calls_api_and_acks(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "log_wellness", spy)
    result = await check_in._do_log_wellness(_data(), mood=4, pain_level=2, notes="ok")
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs == {"mood": 4, "pain_level": 2, "notes": "ok"}
    assert spy.await_args.args[0] == "call-1"
    assert isinstance(result, str) and result  # a spoken acknowledgement


async def test_do_log_wellness_handles_api_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "log_wellness", _boom)
    result = await check_in._do_log_wellness(_data(), mood=3, pain_level=None, notes=None)
    assert isinstance(result, str) and result  # graceful string, no exception


async def test_do_log_medication_calls_api(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "log_medication", spy)
    result = await check_in._do_log_medication(_data(), medication_name="Aspirin", taken=True)
    spy.assert_awaited_once()
    assert spy.await_args.kwargs == {
        "medication_name": "Aspirin",
        "taken": True,
        "reported_time": None,
    }
    assert isinstance(result, str) and result


async def test_do_get_today_meds_formats_list(monkeypatch):
    async def _meds(call_id, settings):
        return [
            {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]},
            {"name": "Metformin", "dosage": None, "times": ["08:00", "20:00"]},
        ]

    monkeypatch.setattr(check_in.api_client, "get_today_meds", _meds)
    result = await check_in._do_get_today_meds(_data())
    assert "Aspirin" in result
    assert "Metformin" in result


async def test_do_get_today_meds_empty(monkeypatch):
    async def _meds(call_id, settings):
        return []

    monkeypatch.setattr(check_in.api_client, "get_today_meds", _meds)
    result = await check_in._do_get_today_meds(_data())
    assert isinstance(result, str) and result  # a "no medications" style message


async def test_do_end_call_reports_says_goodbye_and_hangs_up(monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "report_end_call", report)

    job_ctx = MagicMock()
    job_ctx.delete_room = AsyncMock()
    job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()

    await check_in._do_end_call(_data(job_ctx=job_ctx), session, "check_in_complete")

    report.assert_awaited_once()
    session.say.assert_awaited_once()  # goodbye
    job_ctx.delete_room.assert_awaited_once()  # hang up
    job_ctx.shutdown.assert_called_once()


async def test_do_end_call_hangs_up_even_if_report_fails(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "report_end_call", _boom)
    job_ctx = MagicMock()
    job_ctx.delete_room = AsyncMock()
    job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()

    await check_in._do_end_call(_data(job_ctx=job_ctx), session, "x")

    job_ctx.delete_room.assert_awaited_once()  # report failure must not block hangup
    job_ctx.shutdown.assert_called_once()


def test_build_check_in_agent_attaches_four_tools():
    agent = check_in.build_check_in_agent()
    names = {t.name for t in agent.tools}
    assert names == {"log_wellness", "log_medication", "get_today_meds", "end_call"}
    assert agent.instructions == check_in.CHECK_IN_INSTRUCTIONS
```

> Note on `test_build_check_in_agent_attaches_four_tools`: it asserts `agent.tools` exposes the four `FunctionTool`s with `.name` equal to the function names. If the installed 1.5.14 `Agent` exposes the tool list under a different attribute or the `FunctionTool` name attribute differs, adjust this single assertion to match the real API (the four tools must still be attached) and note it — do not change the production wiring.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.check_in'`.

- [ ] **Step 3: Write the implementation**

Create `services/agent/src/usan_agent/check_in.py`:

```python
"""Outbound wellness check-in: the LLM-driven conversation tools (design spec §4).

Each @function_tool reads per-call state (call_id, settings, JobContext) from the
session's typed userdata (RunContext.userdata) and delegates to a plain _do_*
helper. Helpers catch API errors and return a calm, spoken string so a transient
failure never crashes the call. end_call mirrors leave_voicemail: report → say
goodbye → delete_room → shutdown.
"""

from dataclasses import dataclass
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.settings import Settings

CHECK_IN_INSTRUCTIONS = """You are a warm, patient daily check-in caller from USAN Retirement,
speaking to an elder on the phone. Speak slowly and kindly, one or two short sentences at a time,
and pause for them to answer.

Conduct the check-in in this order, adapting naturally to their answers:
1. Ask how they are feeling today and roughly how their mood is. Record it with `log_wellness`
   (mood 1-5 where 5 is great; include any pain level 0-10 and a short note if they mention it).
2. Use `get_today_meds` to find out which medications they take today, then gently ask whether
   they have taken each one. Record each with `log_medication`.
3. When the check-in is complete, thank them and call `end_call` with a short reason
   (for example "check_in_complete").

Never read out internal IDs or tool names. If a tool reports a problem, reassure them calmly and
continue — do not repeat a failed action more than once.
"""

GOODBYE_MESSAGE = "Thank you for your time today. Take care, and have a wonderful day. Goodbye."


@dataclass
class CheckInData:
    """Per-call state made available to tools via RunContext.userdata."""

    call_id: str
    settings: Settings
    job_ctx: Any  # livekit.agents.JobContext — typed Any to avoid importing the heavy symbol


async def _do_log_wellness(
    data: CheckInData, *, mood: int | None, pain_level: int | None, notes: str | None
) -> str:
    try:
        await api_client.log_wellness(
            data.call_id, data.settings, mood=mood, pain_level=pain_level, notes=notes
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("log_wellness tool failed")
        return "I had a little trouble saving that, but let's keep going."
    return "Thank you, I've noted how you're feeling."


async def _do_log_medication(
    data: CheckInData, *, medication_name: str, taken: bool, reported_time: str | None = None
) -> str:
    try:
        await api_client.log_medication(
            data.call_id,
            data.settings,
            medication_name=medication_name,
            taken=taken,
            reported_time=reported_time,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("log_medication tool failed")
        return "I had trouble noting that medication, but we can continue."
    return "Got it, I've recorded that."


async def _do_get_today_meds(data: CheckInData) -> str:
    try:
        meds = await api_client.get_today_meds(data.call_id, data.settings)
    except Exception:
        logger.bind(call_id=data.call_id).warning("get_today_meds tool failed")
        return "I couldn't look up the medication list right now."
    if not meds:
        return "There are no medications scheduled for today."
    parts = []
    for med in meds:
        name = med.get("name", "a medication")
        times = ", ".join(med.get("times", [])) or "today"
        dosage = med.get("dosage")
        parts.append(f"{name}{f' ({dosage})' if dosage else ''} at {times}")
    return "Today's medications are: " + "; ".join(parts) + "."


async def _do_end_call(data: CheckInData, session: Any, reason: str) -> None:
    """Report the end reason (best-effort), say goodbye, then hang up."""
    try:
        await api_client.report_end_call(data.call_id, data.settings, reason)
    except Exception:
        logger.bind(call_id=data.call_id).warning("report_end_call failed; hanging up anyway")
    handle = session.say(GOODBYE_MESSAGE, allow_interruptions=False, add_to_chat_ctx=False)
    await handle
    await data.job_ctx.delete_room()
    data.job_ctx.shutdown(reason="ended_by_agent")


@function_tool
async def log_wellness(
    ctx: RunContext,
    mood: int | None = None,
    pain_level: int | None = None,
    notes: str | None = None,
) -> str:
    """Record the elder's wellness this call.

    Args:
        mood: Overall mood, 1 (low) to 5 (great).
        pain_level: Pain level, 0 (none) to 10 (severe).
        notes: A short free-text note about how they are doing.
    """
    return await _do_log_wellness(ctx.userdata, mood=mood, pain_level=pain_level, notes=notes)


@function_tool
async def log_medication(
    ctx: RunContext,
    medication_name: str,
    taken: bool,
    reported_time: str | None = None,
) -> str:
    """Record whether the elder has taken a medication.

    Args:
        medication_name: The medication's name.
        taken: True if they have taken it, False if not.
        reported_time: Optional ISO-8601 time they said they took it.
    """
    return await _do_log_medication(
        ctx.userdata, medication_name=medication_name, taken=taken, reported_time=reported_time
    )


@function_tool
async def get_today_meds(ctx: RunContext) -> str:
    """List the medications the elder is scheduled to take today."""
    return await _do_get_today_meds(ctx.userdata)


@function_tool
async def end_call(ctx: RunContext, reason: str = "check_in_complete") -> str:
    """End the call once the check-in is complete.

    Args:
        reason: A short reason, e.g. "check_in_complete".
    """
    await _do_end_call(ctx.userdata, ctx.session, reason)
    return ""


def build_check_in_agent() -> Agent:
    """The outbound check-in Agent with its four in-call tools."""
    return Agent(
        instructions=CHECK_IN_INSTRUCTIONS,
        tools=[log_wellness, log_medication, get_today_meds, end_call],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: PASS (8 cases). If `test_build_check_in_agent_attaches_four_tools` fails only on how the tool list / names are exposed by the installed `Agent`, adjust that one assertion to the real API (keep all four tools attached) and note it.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/check_in.py tests/test_check_in.py
git commit -m "feat(agent): add wellness check-in tools + agent"
```

---

## Task 3: Wire the check-in agent into the outbound flow

**Files:**
- Modify: `services/agent/src/usan_agent/pipeline.py`
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_worker.py`

`build_session` gains an optional `userdata`; the **outbound** entrypoint branch builds a `CheckInData` + the check-in agent and starts the session with both. Inbound is unchanged.

- [ ] **Step 1: Write the failing test**

Create (or append to) `services/agent/tests/test_worker.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker


def _settings(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "c")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "v")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    from usan_agent.settings import get_settings

    get_settings.cache_clear()


async def test_outbound_starts_check_in_agent(monkeypatch):
    _settings(monkeypatch)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        session.on = MagicMock()
        captured["session"] = session
        return session

    built = {}

    def _fake_build_check_in_agent():
        agent = MagicMock(name="check_in_agent")
        built["agent"] = agent
        return agent

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", _fake_build_check_in_agent)
    # Short-circuit the detection window so the test doesn't run the real conversation.
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    # The session was started with a CheckInData userdata scoped to this call.
    data = captured["userdata"]
    assert data is not None
    assert data.call_id == "call-1"
    assert data.job_ctx is ctx
    # The check-in agent (not the greet-only agent) was started.
    captured["session"].start.assert_awaited_once()
    assert captured["session"].start.await_args.kwargs["agent"] is built["agent"]


async def test_inbound_uses_greet_only_agent(monkeypatch):
    _settings(monkeypatch)

    captured = {}

    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "greet", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None  # inbound

    await worker.entrypoint(ctx)

    assert captured["userdata"] is None  # inbound carries no check-in state
```

> Note: if `services/agent/tests/test_worker.py` already exists, append these two tests and reuse any existing helpers instead of redefining `_settings`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_worker.py -k "check_in or greet_only" -v`
Expected: FAIL — `worker` has no `build_check_in_agent`, and `build_session` is called without `userdata`.

- [ ] **Step 3: Add the `userdata` parameter to `build_session`**

In `services/agent/src/usan_agent/pipeline.py`, replace the `build_session` signature + return so it accepts optional userdata. Change:

```python
def build_session(settings: Settings) -> AgentSession[None]:
    """Construct an AgentSession wiring STT, LLM, TTS, VAD, and turn-detector."""
    logger.info("Building AgentSession (cartesia STT/TTS, {model})", model=LLM_MODEL)
    return AgentSession(
        vad=silero.VAD.load(),
```

to:

```python
def build_session(settings: Settings, userdata: Any = None) -> AgentSession[Any]:
    """Construct an AgentSession wiring STT, LLM, TTS, VAD, and turn-detector.

    ``userdata`` (a check_in.CheckInData on outbound calls) is exposed to tools via
    RunContext.userdata; None for greet-only inbound calls.
    """
    logger.info("Building AgentSession (cartesia STT/TTS, {model})", model=LLM_MODEL)
    return AgentSession(
        userdata=userdata,
        vad=silero.VAD.load(),
```

Add `from typing import Any` to the top imports of `pipeline.py`. The `greet` signature stays `async def greet(session: AgentSession[None]) -> None` — but loosen it to `AgentSession[Any]` to match:

```python
async def greet(session: AgentSession[Any]) -> None:
```

- [ ] **Step 4: Wire the outbound branch in `worker.py`**

In `services/agent/src/usan_agent/worker.py`, add the imports:

```python
from usan_agent.check_in import CheckInData, build_check_in_agent
```

Then change the `entrypoint` so the agent + session userdata are chosen by direction. Replace the block that builds the session/agent and starts it (currently `session = build_session(settings)`, `agent = build_agent()`, `await session.start(agent=agent, room=ctx.room)`) with:

```python
    if meta.direction == "outbound" and meta.call_id:
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
        session = build_session(settings, userdata=data)
        agent = build_check_in_agent()
    else:
        session = build_session(settings)
        agent = build_agent()
    await session.start(agent=agent, room=ctx.room)
    log.info("Session started; waiting for participant")
```

Leave the rest of `entrypoint` (the outbound `wait_for_participant` + voicemail window, the inbound greet) unchanged. `build_agent` stays imported and used for the inbound/greet-only path.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: PASS. Then the full suite: `uv run pytest -v` (existing voicemail/pipeline tests still pass).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_agent/pipeline.py src/usan_agent/worker.py tests/test_worker.py
git commit -m "feat(agent): run the check-in agent on outbound calls"
```

---

## Self-Review

**1. Spec coverage (§4.2 agent conversation, §4.1 tool clients):**
- Multi-turn conversation: already automatic post-`session.start()`; this plan supplies the missing pieces — a structured check-in system prompt (`CHECK_IN_INSTRUCTIONS`) and the four tools.
- `get_today_meds` / `log_wellness` / `log_medication` / `end_call` tools → Task 2, each calling the Plan 3a endpoint via the Task 1 client + the per-call JWT.
- Graceful end → `end_call`/`_do_end_call` mirrors `leave_voicemail` (report → goodbye → delete_room → shutdown).
- Per-call auth/state via `userdata` (no globals); tools fail soft (never crash the call).
- Outbound-only is an explicit decision; inbound stays greet-only. DTMF, transcript flush, and inbound conversation are noted out of scope.

**2. Placeholder scan:** Every step has complete code — client functions, tools, helpers, prompt, builder, wiring, and verbatim tests. No "TBD"/"add validation"/"similar to Task N".

**3. Type/name consistency:** `CheckInData(call_id, settings, job_ctx)` is constructed identically in `worker.py` and read as `ctx.userdata` in every `_do_*`; the four client functions (`log_wellness`/`log_medication`/`get_today_meds`/`report_end_call`) match the tool helpers' calls; `build_check_in_agent`/`build_session(settings, userdata=...)` signatures match their call sites and tests.

**LiveKit-API verification points (flagged for the implementer):** three lines depend on installed-1.5.14 specifics that the recon verified but that you should confirm by running the tests, adjusting only the affected assertion/line (never the design) and noting it: (a) `@function_tool` + `Agent(tools=[...])` attachment and how `agent.tools`/`FunctionTool.name` are exposed (Task 2 build test); (b) `RunContext.userdata` returning the `CheckInData`; (c) `AgentSession(userdata=...)` accepting the value and a tool hanging up via `ctx.userdata.job_ctx.delete_room()` + `shutdown()`. All three were confirmed present in 1.5.14 during grounding; the tests are written to surface any mismatch immediately.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-31-plan-3b-agent-conversation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, two-stage review between tasks.

**2. Inline Execution** — execute tasks in this session using executing-plans.

**Notes:** This consumes the Plan 3a `/v1/tools/*` endpoints (PR #7) — execute from a worktree branched off a `main` that has 3a merged, so a live end-to-end test can hit real endpoints (the unit tests here mock the HTTP boundary, so they don't strictly require it). After 3b, the outbound check-in works end to end; remaining arc items are transcript flush (3c) and recording (a deploy-time plan).
