# RetellAI Parity Phase 4b-1 — `create-sms-chat` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve RetellAI's `POST /create-sms-chat` on the compat sub-app — persist an `sms_chat`-typed chat row, send the agent's configured greeting via Telnyx, and return the same `ChatResponse` (HTTP 200, `chat_type: sms_chat`) — reusing the Phase 4a chat tables, serializer, and id-codec.

**Architecture:** Additive only. Migration `0043` adds two nullable columns to `chat_sessions`; a new `chat_service.create_sms_chat` resolves the agent (one-time `override_agent_id`, else the same-org `from_number` → `outbound_sms_agents` binding), substitutes dynamic vars into `config.prompts.greeting`, persists session + the greeting message, sends it via the existing `telnyx_messaging.send_sms`, and commits. Any send failure rolls back the whole transaction (502). No Vertex (the initial message is the static greeting). The Phase 4a `get/list/update/end/delete` paths surface `sms_chat` rows unchanged (the serializer already emits `chat_type`).

**Tech Stack:** FastAPI compat sub-app, SQLAlchemy async + Alembic (Postgres, RLS), Pydantic v2, `retell-sdk==5.53.0` (conformance oracle), `pytest` (parallel by default).

## Global Constraints

- **Oracle is ground truth:** `POST /create-sms-chat` returns **HTTP 200** (NOT 201) → the shared `ChatResponse` component; `chat_type` is `"sms_chat"`. Required request fields: `from_number`, `to_number` (E.164, `minLength 1`). Optional: `override_agent_id`, `override_agent_version`, `metadata`, `retell_llm_dynamic_variables`.
- `response_model_exclude_none=True` on the route; `CompatChat` fields are `| None = None` so empties are omitted.
- **PHI-safe logging:** log only `type(exc).__name__` (via `logger.bind(err=...)`); never log `to_number`, the greeting body, or dynamic vars; re-raise `CompatError(...) from None`.
- **503 before any write** when SMS sending is not configured. **502 with whole-transaction `await db.rollback()`** on any send failure (no orphan row, no half-written PHI).
- RLS `organization_id` is the DB server-default (`COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())`) — **never set by app code**. The `from_number` binding lookup runs under the caller's own org (same-org, RLS-safe).
- `telnyx_messaging.send_sms` is **not modified**. `apps/api` must **not** import `services/agent`.
- All errors are `CompatError(status_code: int, message: str)` (positional). All ids go through `ids.encode_*` / `ids.decode_*`.
- Ends at **squash-merge to `main`**. **No `v*` tag** (inert until an operator deploys migration `0043` + enables Telnyx messaging).

**Branch:** `retell-parity-phase4b-sms-chat` (already created; the design spec is committed there as `e79fe23`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `apps/api/src/usan_api/db/models.py` | modify (`ChatSession`) | + `from_number` / `to_number` nullable columns |
| `apps/api/migrations/versions/0043_chat_sms_numbers.py` | create | additive migration (head was `0042`) |
| `apps/api/src/usan_api/compat/schemas/chats.py` | modify | + `CreateSmsChatRequest` |
| `apps/api/src/usan_api/repositories/chats.py` | modify (`add_session`) | accept `chat_type`/`from_number`/`to_number` |
| `apps/api/src/usan_api/compat/chat_service.py` | modify | + `_sms_send_ready`, `_resolve_sms_agent`, `create_sms_chat`; + `sms_chat` guard in `create_chat_completion` |
| `apps/api/src/usan_api/compat/routers/chats.py` | modify | + `POST /create-sms-chat` route |
| `apps/api/src/usan_api/compat/routers/unsupported.py` | modify | remove the `/create-sms-chat` 501 entry |
| `apps/api/tests/compat/conftest.py` | modify | + `sms_messaging_enabled`, `mock_send_sms` fixtures |
| `apps/api/tests/compat/test_sms_chat_columns.py` | create | Task 1/2 model/migration/repo tests |
| `apps/api/tests/compat/test_create_sms_chat.py` | create | Task 2/3/4 schema + behavioral tests |
| `apps/api/tests/compat/test_freeze_sms_chat.py` | create | Task 5 conformance suite |
| `docs/deployment/sms-chat.md` | create | operator note |

---

### Task 1: Migration 0043 + `ChatSession.from_number`/`to_number`

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (the `ChatSession` class, after the `chat_type` column near line 1289)
- Create: `apps/api/migrations/versions/0043_chat_sms_numbers.py`
- Test: `apps/api/tests/compat/test_sms_chat_columns.py`

**Interfaces:**
- Produces: two new nullable string columns on `chat_sessions` (`from_number: Mapped[str | None]`, `to_number: Mapped[str | None]`), available to Task 2's `add_session`.

- [ ] **Step 1: Write the failing test** — `apps/api/tests/compat/test_sms_chat_columns.py`:

```python
"""ChatSession gains nullable from_number/to_number for sms_chat rows (Phase 4b-1)."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.tenant_context import set_tenant_context


async def _seed_agent_profile(db) -> AgentProfile:
    profile = AgentProfile(
        name=f"SMS Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    return profile


@pytest.mark.asyncio
async def test_chat_session_persists_from_and_to_number(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = await _seed_agent_profile(app_session)

    s = ChatSession(
        agent_profile_id=profile.id,
        agent_version=1,
        chat_type="sms_chat",
        dynamic_vars={},
        from_number="+15550001111",
        to_number="+15550002222",
    )
    app_session.add(s)
    await app_session.flush()

    loaded = await app_session.get(ChatSession, s.id)
    assert loaded is not None
    assert loaded.chat_type == "sms_chat"
    assert loaded.from_number == "+15550001111"
    assert loaded.to_number == "+15550002222"
    await app_session.rollback()


def test_migration_0043_revision_header() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0043_chat_sms_numbers.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0043", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0043"
    assert mod.down_revision == "0042"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_sms_chat_columns.py -q`
Expected: FAIL — `TypeError: 'from_number' is an invalid keyword argument for ChatSession` (and the migration file does not yet exist).

- [ ] **Step 3a: Add the model columns** — `apps/api/src/usan_api/db/models.py`, inside `class ChatSession` immediately after the `chat_type` column (the existing line is `chat_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'api_chat'"))`):

```python
    from_number: Mapped[str | None] = mapped_column(Text)
    to_number: Mapped[str | None] = mapped_column(Text)
```

(`Text` and `Mapped`/`mapped_column` are already imported at the top of the file. `Mapped[str | None]` implies nullable — no `nullable=` kwarg, matching the existing `ended_at`/`archived_at` style.)

- [ ] **Step 3b: Create the migration** — `apps/api/migrations/versions/0043_chat_sms_numbers.py`:

```python
"""chat_sessions: add nullable from_number/to_number for sms_chat rows (Phase 4b-1).

Additive columns on the existing chat_sessions table. Nullable, so api_chat rows are
unaffected and the columns inherit the table's existing usan_app GRANT + RLS policy
(no new grant/policy needed). Owner-DDL migration — the deploy migrates as the usan owner.

Revision ID: 0043
Revises: 0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("from_number", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("to_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "to_number")
    op.drop_column("chat_sessions", "from_number")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_sms_chat_columns.py -q`
Expected: PASS (2 passed).

Then confirm a single linear head:
Run: `cd apps/api && uv run alembic heads`
Expected: `0043 (head)`.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/db/models.py apps/api/migrations/versions/0043_chat_sms_numbers.py apps/api/tests/compat/test_sms_chat_columns.py
git commit -m "feat(api): add nullable from_number/to_number to chat_sessions (Phase 4b-1 migration 0043)"
```

---

### Task 2: `CreateSmsChatRequest` schema + `_sms_send_ready` + parameterize `add_session`

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/chats.py` (after `CreateChatRequest`)
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (add `_sms_send_ready` after `_reject_reserved`)
- Modify: `apps/api/src/usan_api/repositories/chats.py` (`add_session`)
- Test: `apps/api/tests/compat/test_sms_chat_columns.py` (append) + new `apps/api/tests/compat/test_create_sms_chat.py` (schema + helper unit tests)

**Interfaces:**
- Produces:
  - `CreateSmsChatRequest` (Pydantic, `extra="forbid"`): `from_number: str`, `to_number: str` (required, `min_length=1`); `override_agent_id: str | None`, `override_agent_version: int | str | None`, `metadata: dict[str, Any] | None`, `retell_llm_dynamic_variables: dict[str, str] | None`.
  - `chat_service._sms_send_ready(settings: Settings) -> bool`.
  - `chats_repo.add_session(db, *, agent_profile_id, agent_version, dynamic_vars, chat_type="api_chat", from_number=None, to_number=None) -> ChatSession`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/compat/test_sms_chat_columns.py`:

```python
async def test_add_session_sets_chat_type_and_numbers(app_session) -> None:
    from usan_api.repositories import chats as chats_repo

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    profile = await _seed_agent_profile(app_session)

    s = await chats_repo.add_session(
        app_session,
        agent_profile_id=profile.id,
        agent_version=1,
        dynamic_vars={},
        chat_type="sms_chat",
        from_number="+15550001111",
        to_number="+15550002222",
    )
    await app_session.flush()
    assert s.chat_type == "sms_chat"
    assert s.from_number == "+15550001111"
    assert s.to_number == "+15550002222"

    d = await chats_repo.add_session(
        app_session, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await app_session.flush()
    assert d.chat_type == "api_chat"
    assert d.from_number is None
    assert d.to_number is None
    await app_session.rollback()
```

Create `apps/api/tests/compat/test_create_sms_chat.py` with the schema + helper unit tests:

```python
"""Behavioral tests for POST /create-sms-chat and its helpers (Phase 4b-1)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from usan_api.compat.chat_service import _sms_send_ready
from usan_api.compat.schemas.chats import CreateSmsChatRequest
from usan_api.settings import get_settings


def test_request_requires_from_and_to() -> None:
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(to_number="+15551234567")  # missing from_number
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(from_number="+15550000000")  # missing to_number


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(
            from_number="+15550000000", to_number="+15551234567", bogus="x"
        )


def test_request_accepts_optionals() -> None:
    req = CreateSmsChatRequest(
        from_number="+15550000000",
        to_number="+15551234567",
        override_agent_id="agent_deadbeef",
        metadata={"crm": 1},
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert req.override_agent_id == "agent_deadbeef"


def _settings_with(**overrides):
    return get_settings().model_copy(update=overrides)


def test_sms_send_ready_truth_table() -> None:
    ready = _settings_with(
        telnyx_messaging_enabled=True,
        telnyx_messaging_api_key=SecretStr("k"),
        telnyx_messaging_profile_id="p",
        telnyx_from_number="+15550000000",
    )
    assert _sms_send_ready(ready) is True
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_enabled": False})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_api_key": None})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_profile_id": None})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_from_number": None})) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py tests/compat/test_sms_chat_columns.py::test_add_session_sets_chat_type_and_numbers -q`
Expected: FAIL — `ImportError: cannot import name 'CreateSmsChatRequest'` / `'_sms_send_ready'`; and `add_session() got an unexpected keyword argument 'chat_type'`.

- [ ] **Step 3a: Add the schema** — `apps/api/src/usan_api/compat/schemas/chats.py`, after `CreateChatRequest` (the `Any`/`Field`/`ConfigDict`/`BaseModel` imports already exist):

```python
class CreateSmsChatRequest(BaseModel):
    """POST /create-sms-chat. Oracle: from_number + to_number required; the rest optional."""

    model_config = ConfigDict(extra="forbid")

    from_number: str = Field(min_length=1)
    to_number: str = Field(min_length=1)
    override_agent_id: str | None = Field(default=None, min_length=1)
    override_agent_version: int | str | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None
```

- [ ] **Step 3b: Add `_sms_send_ready`** — `apps/api/src/usan_api/compat/chat_service.py`, immediately after `_reject_reserved` (lines 41–43). `Settings` is already imported:

```python
def _sms_send_ready(settings: Settings) -> bool:
    """True iff outbound SMS can be sent: the feature flag is on and the three Telnyx
    messaging secrets are present. The 503 gate in create_sms_chat checks this before any
    write (send_sms itself would otherwise raise after a row was already written)."""
    return bool(
        settings.telnyx_messaging_enabled
        and settings.telnyx_messaging_api_key
        and settings.telnyx_messaging_profile_id
        and settings.telnyx_from_number
    )
```

- [ ] **Step 3c: Parameterize `add_session`** — `apps/api/src/usan_api/repositories/chats.py`, replace the existing `add_session` (lines 18–33) with:

```python
async def add_session(
    db: AsyncSession,
    *,
    agent_profile_id: uuid.UUID,
    agent_version: int,
    dynamic_vars: dict[str, Any],
    chat_type: str = "api_chat",
    from_number: str | None = None,
    to_number: str | None = None,
) -> ChatSession:
    session = ChatSession(
        agent_profile_id=agent_profile_id,
        agent_version=agent_version,
        status=ChatStatus.ONGOING,
        chat_type=chat_type,
        dynamic_vars=dynamic_vars,
        from_number=from_number,
        to_number=to_number,
    )
    db.add(session)
    return session
```

(Existing `create_chat` callers pass no `chat_type`/numbers → the defaults preserve api_chat behavior exactly.)

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py tests/compat/test_sms_chat_columns.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/chats.py apps/api/src/usan_api/compat/chat_service.py apps/api/src/usan_api/repositories/chats.py apps/api/tests/compat/test_create_sms_chat.py apps/api/tests/compat/test_sms_chat_columns.py
git commit -m "feat(api): CreateSmsChatRequest schema, _sms_send_ready, parameterized add_session (Phase 4b-1)"
```

---

### Task 3: `create_sms_chat` service + route + remove the 501 entry + shared fixtures

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (imports + `_resolve_sms_agent` + `create_sms_chat`)
- Modify: `apps/api/src/usan_api/compat/routers/chats.py` (import + route)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove lines 45–46)
- Modify: `apps/api/tests/compat/conftest.py` (add `sms_messaging_enabled`, `mock_send_sms` fixtures)
- Test: `apps/api/tests/compat/test_create_sms_chat.py` (append HTTP tests)

**Interfaces:**
- Consumes: `CreateSmsChatRequest`, `_sms_send_ready` (Task 2); `chats_repo.add_session(..., chat_type, from_number, to_number)` (Task 2); `telnyx_messaging.send_sms(settings, *, to_number, body) -> str` (raises `TelnyxMessagingError`); `phone_numbers_repo.get_by_e164(db, phone_e164) -> PhoneNumber | None`; `agent_profiles_repo.is_live_profile`/`get_profile`; `_load_published_config(db, profile_id) -> AgentConfig`; `ids.decode_agent_id`/`encode_chat_id`; `_serialize_full(db, session)`.
- Produces: `chat_service.create_sms_chat(db, settings: Settings, body: CreateSmsChatRequest) -> ChatSession`; `POST /create-sms-chat` (200); fixtures `sms_messaging_enabled` (yields the configured sender `"+15550000000"`) and `mock_send_sms` (records calls).

- [ ] **Step 1: Add the shared fixtures first** — `apps/api/tests/compat/conftest.py` (append; mirrors the `gcp_project_set` override mechanism and the `test_sms_outbox` monkeypatch target):

```python
_SMS_FROM = "+15550000000"


@pytest.fixture
def sms_messaging_enabled(compat_client):
    """Override get_settings on the mounted compat sub-app so SMS sending is 'configured'.
    Yields the provisioned sender number tests must use as from_number."""
    from pydantic import SecretStr

    from usan_api.settings import Settings
    from usan_api.settings import get_settings as _get_settings

    compat_app = _get_compat_app(compat_client)
    base = _get_settings()

    def _override() -> Settings:
        return base.model_copy(
            update={
                "telnyx_messaging_enabled": True,
                "telnyx_messaging_api_key": SecretStr("test-key"),
                "telnyx_messaging_profile_id": "test-profile",
                "telnyx_from_number": _SMS_FROM,
            }
        )

    compat_app.dependency_overrides[_get_settings] = _override
    yield _SMS_FROM
    compat_app.dependency_overrides.pop(_get_settings, None)


@pytest.fixture
def mock_send_sms(monkeypatch):
    """Patch telnyx_messaging.send_sms (where create_sms_chat looks it up). Records calls."""
    from usan_api import telnyx_messaging

    calls: list[dict[str, str]] = []

    async def _fake(settings, *, to_number: str, body: str) -> str:
        calls.append({"to_number": to_number, "body": body})
        return "msg-test-123"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake)
    return calls
```

(`_get_compat_app`, `pytest`, `compat_client` already live in this conftest. `get_settings` is the same symbol the `gcp_project_set` fixture overrides — these two fixtures must not be combined in one test, and SMS tests never need gcp.)

- [ ] **Step 2: Write the failing HTTP tests** — append to `apps/api/tests/compat/test_create_sms_chat.py`:

```python
import asyncio
import json

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_SMS_FROM = "+15550000000"
_SMS_TO = "+15551234567"


def _create_sms(client, headers, *, from_number=_SMS_FROM, to_number=_SMS_TO, **extra):
    return client.post(
        "/create-sms-chat",
        json={"from_number": from_number, "to_number": to_number, **extra},
        headers=headers,
    )


def test_create_sms_chat_requires_key(compat_client):
    r = compat_client.post("/create-sms-chat", json={"from_number": _SMS_FROM, "to_number": _SMS_TO})
    assert r.status_code == 401


def test_503_when_messaging_disabled(compat_client, compat_headers, web_agent_id):
    # default settings: telnyx_messaging_enabled is False -> 503 before any write
    r = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert r.status_code == 503, r.text


def test_422_from_number_not_provisioned(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(
        compat_client, compat_headers, from_number="+19998887777", override_agent_id=web_agent_id
    )
    assert r.status_code == 422, r.text
    assert mock_send_sms == []  # never sent


def test_422_no_agent_bound(compat_client, compat_headers, sms_messaging_enabled, mock_send_sms):
    # provisioned sender, no override and no phone-number binding -> 422
    r = _create_sms(compat_client, compat_headers)
    assert r.status_code == 422, r.text
    assert mock_send_sms == []


def test_200_with_override_agent_id(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(
        compat_client,
        compat_headers,
        override_agent_id=web_agent_id,
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "sms_chat"
    assert body["chat_id"].startswith("chat_")
    assert len(mock_send_sms) == 1
    assert mock_send_sms[0]["to_number"] == _SMS_TO
    assert mock_send_sms[0]["body"]  # the greeting was sent


def _seed_phone_binding(dsn: str, from_number: str, agent_id: str) -> None:
    async def _run() -> None:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa_text(
                        "INSERT INTO phone_numbers (phone_e164, phone_number_type, "
                        "outbound_sms_agents) VALUES (:e164, 'custom', CAST(:agents AS JSONB))"
                    ),
                    {
                        "e164": from_number,
                        "agents": json.dumps([{"agent_id": agent_id, "weight": 1.0}]),
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_200_with_outbound_sms_binding(
    compat_client,
    compat_headers,
    web_agent_id,
    sms_messaging_enabled,
    mock_send_sms,
    async_database_url,
):
    _seed_phone_binding(async_database_url, _SMS_FROM, web_agent_id)
    r = _create_sms(compat_client, compat_headers)  # no override_agent_id -> use the binding
    assert r.status_code == 200, r.text
    assert r.json()["chat_type"] == "sms_chat"
    assert len(mock_send_sms) == 1


def test_502_on_send_failure_rolls_back(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, monkeypatch
):
    from usan_api import telnyx_messaging

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("send failed")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)

    r = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert r.status_code == 502, r.text

    # rollback proof: no chat row persisted (fresh truncated DB -> list-chats is empty)
    listed = compat_client.post("/v3/list-chats", json={}, headers=compat_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py -q -k "requires_key or 503 or 422 or 200 or 502"`
Expected: FAIL — `/create-sms-chat` currently returns **501** (still a stub), so these assertions fail.

- [ ] **Step 4a: Add the service** — `apps/api/src/usan_api/compat/chat_service.py`.

Extend the imports: add `CreateSmsChatRequest` to the `from usan_api.compat.schemas.chats import (...)` block, and add these two module imports alongside the existing repo aliases:

```python
from usan_api import telnyx_messaging
from usan_api.repositories import phone_numbers as phone_numbers_repo
```

Add the resolver + the op (place after `create_chat`, before `get_chat`):

```python
async def _resolve_sms_agent(db: AsyncSession, body: CreateSmsChatRequest) -> uuid.UUID:
    """override_agent_id wins (one-time override). Otherwise honor the from_number's
    outbound_sms_agents[0] binding WITHIN the caller's org (RLS-safe, same-org — not the
    deferred cross-org inbound case). 422 if no live agent resolves."""
    if body.override_agent_id:
        profile_id = ids.decode_agent_id(body.override_agent_id)
    else:
        pn = await phone_numbers_repo.get_by_e164(db, body.from_number)
        agents = (pn.outbound_sms_agents if pn is not None else None) or []
        token = (agents[0] or {}).get("agent_id") if agents else None
        if not isinstance(token, str) or not token:
            raise CompatError(422, "no agent bound to from_number")
        profile_id = ids.decode_agent_id(token)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent must reference a published agent")
    return profile_id


async def create_sms_chat(
    db: AsyncSession, settings: Settings, body: CreateSmsChatRequest
) -> ChatSession:
    # 1) config gate — BEFORE any write
    if not _sms_send_ready(settings):
        raise CompatError(503, "sms messaging is not configured")
    # 2) from_number must be our single provisioned sender
    if body.from_number != settings.telnyx_from_number:
        raise CompatError(422, "from_number is not a provisioned sender")
    # 3) resolve the agent (override -> same-org binding -> 422), then guard reserved vars
    profile_id = await _resolve_sms_agent(db, body)
    _reject_reserved(body.retell_llm_dynamic_variables)
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    assert profile is not None  # is_live_profile guaranteed
    assert profile.published_version is not None  # is_live_profile guaranteed
    # 4) initial message = the agent's configured greeting with dynamic vars substituted
    cfg = await _load_published_config(db, profile_id)
    values = build_vars({}, body.retell_llm_dynamic_variables or {}, timezone="", now=datetime.now(UTC))
    greeting = substitute(cfg.prompts.greeting, values)
    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    # 5) persist session + greeting, send via Telnyx; ANY failure rolls back the whole txn
    try:
        session = await chats_repo.add_session(
            db,
            agent_profile_id=profile_id,
            agent_version=profile.published_version,
            dynamic_vars=packed,
            chat_type="sms_chat",
            from_number=body.from_number,
            to_number=body.to_number,
        )
        await db.flush()
        seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=seq, role="agent", content=greeting
        )
        await db.flush()
        await telnyx_messaging.send_sms(settings, to_number=body.to_number, body=greeting)
    except CompatError:
        raise
    except Exception as exc:
        # PHI/secret-safe: type name only; discard the whole uncommitted txn (no orphan row).
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("create sms chat failed")
        raise CompatError(502, "sms send failed") from None
    # 6) commit; the router serializes (incl. the sent greeting) via _serialize_full
    await db.commit()
    return session
```

- [ ] **Step 4b: Add the route** — `apps/api/src/usan_api/compat/routers/chats.py`. Add `CreateSmsChatRequest` to the `from usan_api.compat.schemas.chats import (...)` block, then add the route after `create_chat` (note **200**, not 201; `Settings`/`get_settings` are already imported):

```python
@router.post(
    "/create-sms-chat",
    status_code=status.HTTP_200_OK,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def create_sms_chat(
    body: CreateSmsChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChat:
    session = await chat_service.create_sms_chat(db, settings, body)
    _audit(request, "create-sms-chat", ids.encode_chat_id(session.id))
    return await _serialize_full(db, session)
```

- [ ] **Step 4c: Remove the 501 entry** — `apps/api/src/usan_api/compat/routers/unsupported.py`, delete both lines 45–46 (the `# --- Chat ---` comment and `("POST", "/create-sms-chat"),`), since no other entry sits under that header.

- [ ] **Step 5: Run to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py -q`
Expected: PASS (all behavioral tests).

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/compat/chat_service.py apps/api/src/usan_api/compat/routers/chats.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/conftest.py apps/api/tests/compat/test_create_sms_chat.py
git commit -m "feat(api): serve POST /create-sms-chat (live initial send via Telnyx, behind flags) (Phase 4b-1)"
```

---

### Task 4: `create-chat-completion` rejects `sms_chat` + verify 501-coverage

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py` (`create_chat_completion`)
- Test: `apps/api/tests/compat/test_create_sms_chat.py` (append)

**Interfaces:**
- Consumes: the `POST /create-sms-chat` route (Task 3), `sms_messaging_enabled`, `mock_send_sms`.
- Produces: `create_chat_completion` raises `CompatError(422, "cannot complete an sms chat")` for an `sms_chat` session.

- [ ] **Step 1: Write the failing test** — append to `apps/api/tests/compat/test_create_sms_chat.py`:

```python
def test_completion_rejects_sms_chat(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    created = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert created.status_code == 200, created.text
    chat_id = created.json()["chat_id"]

    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py::test_completion_rejects_sms_chat -q`
Expected: FAIL — without the guard the completion path proceeds to the gcp 503 gate (or attempts a turn), returning 503/500, not 422.

- [ ] **Step 3: Add the guard** — `apps/api/src/usan_api/compat/chat_service.py`, inside `create_chat_completion`, immediately after the `404` lock check (`if session is None: raise CompatError(404, ...)`) and before the `# 2) gate on status` comment:

```python
    # reject api_chat-style synchronous completion on an sms_chat (SMS replies are
    # webhook-driven; never injected through this endpoint). 4b-2 will drive sms replies.
    if session.chat_type == "sms_chat":
        raise CompatError(422, "cannot complete an sms chat")
```

- [ ] **Step 4: Run to verify it passes + 501-coverage stays green**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_sms_chat.py::test_completion_rejects_sms_chat tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -q`
Expected: PASS. (`test_surface_coverage` auto-passes now that `/create-sms-chat` is route-served and removed from `_UNSUPPORTED`; `test_compat_fidelity` does not list `/create-sms-chat`, so it stays green.)

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/chat_service.py apps/api/tests/compat/test_create_sms_chat.py
git commit -m "feat(api): reject create-chat-completion on sms_chat sessions (Phase 4b-1)"
```

---

### Task 5: Conformance freeze suite + deployment doc

**Files:**
- Create: `apps/api/tests/compat/test_freeze_sms_chat.py`
- Create: `docs/deployment/sms-chat.md`

**Interfaces:**
- Consumes: `compat_client`, `compat_headers`, `web_agent_id`, `sms_messaging_enabled`, `mock_send_sms`; `assert_conforms` / `assert_sdk_roundtrip` from `apps/api/tests/compat/conformance.py` (the `"ChatResponse"` / `"retell.types:ChatResponse"` entries already exist from Phase 4a — `sms_chat` reuses them, so **no `conformance.py` change**).

- [ ] **Step 1: Write the conformance suite** — `apps/api/tests/compat/test_freeze_sms_chat.py`:

```python
"""create-sms-chat conformance freeze (Phase 4b-1): SDK round-trip + get/list visibility."""

from __future__ import annotations

from .conformance import assert_conforms, assert_sdk_roundtrip

_SMS_FROM = "+15550000000"
_SMS_TO = "+15551234567"


def _create_sms(client, headers, agent_id):
    return client.post(
        "/create-sms-chat",
        json={
            "from_number": _SMS_FROM,
            "to_number": _SMS_TO,
            "override_agent_id": agent_id,
            "retell_llm_dynamic_variables": {"name": "Pat"},
        },
        headers=headers,
    )


def test_create_sms_chat_conforms(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(compat_client, compat_headers, web_agent_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "sms_chat"
    assert body["chat_id"].startswith("chat_")
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")


def test_sms_chat_visible_via_get_and_list(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    chat_id = _create_sms(compat_client, compat_headers, web_agent_id).json()["chat_id"]

    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers)
    assert got.status_code == 200, got.text
    assert got.json()["chat_type"] == "sms_chat"
    assert_sdk_roundtrip(got.json(), "retell.types:ChatResponse")

    listed = compat_client.post("/v3/list-chats", json={}, headers=compat_headers)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(i["chat_id"] == chat_id and i["chat_type"] == "sms_chat" for i in items)
    # V3 list items omit transcript / message_with_tool_calls
    item = next(i for i in items if i["chat_id"] == chat_id)
    assert "transcript" not in item
    assert "message_with_tool_calls" not in item
```

- [ ] **Step 2: Run to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_sms_chat.py -q`
Expected: PASS.

- [ ] **Step 3: Write the deployment doc** — `docs/deployment/sms-chat.md`:

```markdown
# create-sms-chat (Phase 4b-1) — operator note

RetellAI-compatible `POST /compat/create-sms-chat`. Persists an `sms_chat` chat row and
sends the agent's configured greeting via Telnyx. Inert until configured.

## Enable
1. Apply migration `0043` (adds nullable `from_number`/`to_number` to `chat_sessions`).
   Owner-DDL — the deploy migrates as the `usan` owner before `compose up` (see the
   migrations-need-owner runbook).
2. Set in the VM `.env` (already wired into compose) and the Secret Manager env:
   - `TELNYX_MESSAGING_ENABLED=true`
   - `TELNYX_MESSAGING_API_KEY=...`
   - `TELNYX_MESSAGING_PROFILE_ID=...`
   - `TELNYX_FROM_NUMBER=+1...`  (the single provisioned sender)
3. Mint a compat key (super-admin UI) — all compat ops 401 until a key exists.

Until step 2, `create-sms-chat` returns **503**. No `GCP_PROJECT` is needed (no Vertex in 4b-1).

## Behavior
- Request: `from_number` (must equal `TELNYX_FROM_NUMBER`, else 422), `to_number`, optional
  `override_agent_id` / `override_agent_version` / `metadata` / `retell_llm_dynamic_variables`.
- Agent: `override_agent_id` wins; otherwise the `from_number`'s `outbound_sms_agents[0]`
  binding (same-org) is honored; else 422.
- Returns 200 + `ChatResponse` (`chat_type: sms_chat`). The chat is gettable/listable via the
  Phase 4a chat ops. `create-chat-completion` on an sms_chat returns 422.

## Caveats / deferred
- **Orphan window:** if the Telnyx send succeeds but the commit then fails, an SMS was sent
  with no persisted row (tiny window; an idempotency key is deferred).
- **No inbound replies yet:** Phase 4b-2 adds the inbound Telnyx webhook → match the open
  sms_chat (default-org) → Vertex reply → send-back, plus per-message `telnyx_message_id` dedup.
- Multi-tenant cross-org inbound routing, weighted binding selection, and multi-number sending
  are deferred.
```

- [ ] **Step 4: Run the full compat suite to confirm no regressions**

Run: `cd apps/api && uv run pytest tests/compat -q`
Expected: PASS (parallel).

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/compat/test_freeze_sms_chat.py docs/deployment/sms-chat.md
git commit -m "test(api): create-sms-chat conformance freeze + deployment note (Phase 4b-1)"
```

---

## Final verification (after all tasks)

```bash
cd apps/api && uv run pytest -q          # full api suite, parallel
cd apps/api && uv run ruff check . && uv run ruff format --check .
cd apps/api && uv run mypy               # files=src (never `mypy .`)
cd apps/api && uv run alembic heads      # -> 0043 (head)
```

Then squash-merge the `retell-parity-phase4b-sms-chat` branch to `main` (no `v*` tag).

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- §3 oracle (200, ChatResponse, chat_type) → Global Constraints + Task 3 (route 200) + Task 5 (assert_conforms/roundtrip).
- §4 data model (0043, from/to) → Task 1.
- §5 Telnyx send (unchanged) → Task 3 (calls `send_sms`; never edits it).
- §6 service flow (503-first, 422 gates, flush, send, 502 rollback, commit) → Task 3.
- §7 schema → Task 2.
- §8 free reuse (get/list/etc.) → Task 5 (get/list visibility test); serializer untouched.
- §9 completion guard → Task 4.
- §10 router + remove 501 → Task 3.
- §11 config posture (503-when-off) → Task 3 test + Task 5 doc.
- §12 PHI (type-name log, rollback, audit) → Task 3 service.
- §13 testing (surface coverage both files, SDK round-trip, 503/422, get/list, completion 422, send mocked + 502 rollback) → Tasks 3/4/5.
- §14 deviations / §15 task list → reflected in the 5 tasks + the deployment doc.

**2. Placeholder scan** — none. Every code step shows full code; every test step shows full test bodies; no "TBD"/"similar to"/"add error handling".

**3. Type consistency** — `create_sms_chat(db, settings, body) -> ChatSession` (route calls `_serialize_full` on the returned session, exactly like `create_chat`); `add_session` new kwargs (`chat_type`/`from_number`/`to_number`) match Task 2's signature and the Task 1 model columns; `_sms_send_ready(settings) -> bool` consistent across Task 2 definition and Task 3 use; `_resolve_sms_agent(db, body) -> uuid.UUID`; `telnyx_messaging.send_sms(settings, *, to_number, body)` and `phone_numbers_repo.get_by_e164(db, phone_e164)` match the verbatim source. The route uses `status.HTTP_200_OK` (oracle 200), not the 201 used by `create-chat`.

---

## Execution Handoff

Recommended: **Subagent-Driven Development** — fresh subagent per task, task review (spec + quality) between tasks, broad whole-branch review at the end. Ends at squash-merge to `main`, no `v*` tag.
