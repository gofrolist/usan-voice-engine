# RetellAI Parity Phase 3 — Web Calls (LiveKit WebRTC) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `POST /v2/create-web-call` as a live, end-to-end LiveKit WebRTC call — the response carries a real browser `access_token`, the agent answers the browser participant.

**Architecture:** Mirror the existing test-audio path (room + agent dispatch + browser token) but as a persisted `Call`. A new `call_type` enum column discriminates web vs phone; the shared `serialize_call` mints the token + omits phone fields for web rows; a new SIP-free `dispatch_web_agent` dispatches the worker; the worker gains a `_run_web` branch (outbound minus SIP minus voicemail).

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, Postgres native enums, LiveKit (`livekit-api`, `livekit-agents`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-26-retell-parity-phase3-web-calls-design.md`.

## Global Constraints

- **Oracle response:** `POST /v2/create-web-call` → **201** `V2WebCallResponse` =
  `allOf [{required:[call_type, access_token]}, V2CallBase]`. `V2CallBase` required:
  `call_id, agent_id, agent_version, call_status`. `call_status` of a new web call =
  `"registered"`. `call_type` = `"web_call"`.
- **Omit phone-only fields for web:** `from_number`, `to_number`, `direction`,
  `telephony_identifier` exist only on `V2PhoneCallResponse` — a web body MUST omit all
  four (via `response_model_exclude_none=True`).
- **SDK round-trip target:** `retell.types:WebCallResponse` (the concrete class, NOT the
  `CallResponse` Union). `access_token: str` and `agent_version: int` are required there.
- **Tokens are bearer credentials:** minted on demand, **never stored** in the DB,
  **never logged**.
- **PHI/secret-safe logging:** error paths log `type(exc).__name__` only — never
  `str(exc)`, request/response bodies, metadata, or tokens.
- **Compat session is not autocommit** → explicit `await db.commit()` after a mutation.
- **CI mypy:** `uv run mypy` (config `files=["src"]`) — never `mypy .`.
- **Commit format:** `type(scope): description`, scopes `api` / `agent`. No attribution
  footer (disabled globally).
- **No `v* tag`** — Phase 3 stays inert until an operator deploys migration 0041.
- **Surface gate:** `KNOWN_GAPS` in `tests/compat/test_surface_coverage.py` stays
  `frozenset()`. Moving `/v2/create-web-call` 501→served must keep BOTH
  `tests/compat/test_surface_coverage.py` and `tests/test_compat_fidelity.py` green —
  run the FULL `apps/api` suite, not just `tests/compat`.
- **Run tests from `apps/api`:** `cd apps/api && uv run pytest ...`. Worker tests:
  `cd services/agent && uv run pytest ...`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `apps/api/src/usan_api/db/base.py` | add `CallType` enum | 1 |
| `apps/api/src/usan_api/db/models.py` | add `Call.call_type` column | 1 |
| `apps/api/migrations/versions/0041_call_type.py` | create enum type + column | 1 |
| `apps/api/src/usan_api/compat/serialization.py` | `pack_unhonored` + strip in `unpack_dynamic_vars` | 2 |
| `apps/api/src/usan_api/compat/schemas/calls.py` | `CompatCall.access_token`/optional `direction`; `CreateWebCallRequest` | 2,3 |
| `apps/api/src/usan_api/compat/call_serializer.py` | web branch (mint token, omit phone fields) | 2 |
| `apps/api/src/usan_api/livekit_dispatch.py` | `dispatch_web_agent` + `_web_metadata` | 4 |
| `apps/api/src/usan_api/compat/call_create.py` | `create_web_call` service | 4 |
| `apps/api/src/usan_api/compat/routers/calls.py` | `POST /v2/create-web-call` route | 5 |
| `apps/api/src/usan_api/compat/routers/unsupported.py` | remove web-call from `_UNSUPPORTED` | 5 |
| `apps/api/tests/test_compat_fidelity.py` | remove web-call from 501 parametrize | 5 |
| `apps/api/tests/compat/test_freeze_web_calls.py` | conformance freeze test | 5 |
| `apps/api/tests/compat/conftest.py` | `mock_web_dispatch` fixture + `web_agent_id` | 5 |
| `services/agent/src/usan_agent/worker.py` | `CallMetadata.call_type` + routing + `_run_web` | 6 |
| `services/agent/tests/test_web_session.py` | worker web-branch test | 6 |
| `docs/deployment/web-calls-livekit-url.md` | browser-interop caveat | 7 |

---

### Task 1: `CallType` enum + `Call.call_type` column + migration 0041

**Files:**
- Modify: `apps/api/src/usan_api/db/base.py`
- Modify: `apps/api/src/usan_api/db/models.py`
- Create: `apps/api/migrations/versions/0041_call_type.py`
- Test: `apps/api/tests/test_call_type_model.py`

**Interfaces:**
- Produces: `CallType.PHONE_CALL = "phone_call"`, `CallType.WEB_CALL = "web_call"`;
  `Call.call_type: Mapped[CallType]` (NOT NULL, server-default `phone_call`).

- [ ] **Step 1: Write the failing test** — `apps/api/tests/test_call_type_model.py`

```python
"""Call.call_type discriminator: enum values + phone-default backfill."""

from __future__ import annotations

import pytest

from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call


def test_call_type_enum_values() -> None:
    assert CallType.PHONE_CALL.value == "phone_call"
    assert CallType.WEB_CALL.value == "web_call"


@pytest.mark.asyncio
async def test_new_call_defaults_to_phone_call(db_session) -> None:
    # A Call created WITHOUT call_type takes the server_default 'phone_call'.
    call = Call(direction=CallDirection.OUTBOUND, status=CallStatus.QUEUED)
    db_session.add(call)
    await db_session.flush()
    await db_session.refresh(call)
    assert call.call_type is CallType.PHONE_CALL


@pytest.mark.asyncio
async def test_web_call_type_round_trips(db_session) -> None:
    call = Call(
        direction=CallDirection.INBOUND, status=CallStatus.REGISTERED, call_type=CallType.WEB_CALL
    )
    db_session.add(call)
    await db_session.flush()
    await db_session.refresh(call)
    assert call.call_type is CallType.WEB_CALL
```

> Use the project's existing async DB session fixture. If it is named other than
> `db_session` (check `apps/api/tests/conftest.py`), use that name verbatim.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_call_type_model.py -q`
Expected: FAIL with `ImportError: cannot import name 'CallType'`.

- [ ] **Step 3: Add the enum** — in `apps/api/src/usan_api/db/base.py`, after the
  `CallStatus` class:

```python
class CallType(enum.Enum):
    PHONE_CALL = "phone_call"
    WEB_CALL = "web_call"
```

- [ ] **Step 4: Add the column** — in `apps/api/src/usan_api/db/models.py`: add
  `CallType` to the base import (`from usan_api.db.base import AdminRole, Base,
  CallDirection, CallStatus, CallType, InviteStatus, ProfileStatus`), then add to the
  `Call` class right after the `status` column (around line 143):

```python
    call_type: Mapped[CallType] = mapped_column(
        SAEnum(
            CallType,
            name="call_type",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=CallType.PHONE_CALL.value,
    )
```

- [ ] **Step 5: Write the migration** — `apps/api/migrations/versions/0041_call_type.py`

```python
"""call_type: discriminator column on calls for the web-call surface.

Adds a native enum ``call_type`` (phone_call | web_call) and the ``calls.call_type``
column, NOT NULL with server_default 'phone_call' so the populated table backfills and
every existing phone path keeps working. The ``calls`` table already grants CRUD to
usan_app (column inherits it); the new enum TYPE needs no grant. Owner DDL — the deploy
runs alembic as the `usan` table owner before `compose up`.

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    call_type = postgresql.ENUM("phone_call", "web_call", name="call_type", create_type=False)
    call_type.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "calls",
        sa.Column("call_type", call_type, nullable=False, server_default="phone_call"),
    )


def downgrade() -> None:
    op.drop_column("calls", "call_type")
    postgresql.ENUM(name="call_type", create_type=False).drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_call_type_model.py -q`
Expected: PASS (3 passed). The DB-backed tests exercise the migration via the test
container's `alembic upgrade head`.

- [ ] **Step 7: Lint + type-check**

Run: `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/db/base.py apps/api/src/usan_api/db/models.py \
  apps/api/migrations/versions/0041_call_type.py apps/api/tests/test_call_type_model.py
git commit -m "feat(api): add Call.call_type discriminator + migration 0041"
```

---

### Task 2: serializer web branch — `access_token` + omit phone fields + un-honored strip

**Files:**
- Modify: `apps/api/src/usan_api/compat/serialization.py`
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py`
- Modify: `apps/api/src/usan_api/compat/call_serializer.py`
- Test: `apps/api/tests/compat/test_web_serializer.py`

**Interfaces:**
- Consumes: `CallType` (Task 1); `mint_browser_token(settings, *, room, identity)` from
  `usan_api.livekit_dispatch`; `ids.encode_call_id`.
- Produces: `CompatCall` now has `access_token: str | None = None` and
  `direction: str | None = None`; `serialize_call` emits `call_type="web_call"` + a
  minted token + omits phone fields for `CallType.WEB_CALL`. `serialization.pack_unhonored(...)`
  + `unpack_dynamic_vars` strips `__meta_unhonored__`.

- [ ] **Step 1: Write the failing serializer test** — `apps/api/tests/compat/test_web_serializer.py`

```python
"""serialize_call web branch: web_call type, minted token, phone fields omitted."""

from __future__ import annotations

import pytest

from usan_api.compat import call_serializer
from usan_api.compat.serialization import pack_unhonored, unpack_dynamic_vars
from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call


def test_unpack_strips_unhonored_blob() -> None:
    packed = pack_unhonored(
        {"k": "v", "__meta__": '{"name": "Pat"}'},
        agent_override={"voice_id": "x"},
        current_node_id="n1",
        current_state=None,
    )
    dynamic_vars, metadata = unpack_dynamic_vars(packed)
    assert dynamic_vars == {"k": "v"}          # bare user vars only
    assert metadata == {"name": "Pat"}          # echoed metadata pristine
    assert "__meta_unhonored__" not in dynamic_vars
    assert "__meta_unhonored__" not in metadata


@pytest.mark.asyncio
async def test_serialize_web_call_shape(db_session, settings, published_profile) -> None:
    # published_profile: a fixture giving a published AgentProfile (id + published_version).
    call = Call(
        direction=CallDirection.INBOUND,
        status=CallStatus.REGISTERED,
        call_type=CallType.WEB_CALL,
        profile_override=published_profile.id,
        livekit_room="usan-web-abc123",
        dynamic_vars={"greeting": "hi"},
    )
    db_session.add(call)
    await db_session.flush()

    out = await call_serializer.serialize_call(
        db_session, call, settings, client_host="1.2.3.4"
    )
    dumped = out.model_dump(exclude_none=True)
    assert dumped["call_type"] == "web_call"
    assert isinstance(dumped["access_token"], str) and dumped["access_token"]
    assert dumped["call_status"] == "registered"
    assert isinstance(dumped["agent_version"], int)
    for phone_field in ("from_number", "to_number", "direction", "telephony_identifier"):
        assert phone_field not in dumped
```

> Reuse whatever fixtures the existing serializer/compat tests use for `settings` and a
> published profile (grep `apps/api/tests/compat/conftest.py` and `tests/conftest.py`).
> If no `published_profile` fixture exists, build one inline via the agent-profiles repo
> the way `tests/compat/test_freeze_calls.py` seeds a published agent.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_web_serializer.py -q`
Expected: FAIL — `ImportError: cannot import name 'pack_unhonored'`.

- [ ] **Step 3: Add `pack_unhonored` + strip** — in `apps/api/src/usan_api/compat/serialization.py`,
  after `_META_KEY`:

```python
_UNHONORED_KEY = "__meta_unhonored__"


def pack_unhonored(
    packed: dict[str, Any],
    *,
    agent_override: dict[str, Any] | None,
    current_node_id: str | None,
    current_state: str | None,
) -> dict[str, Any]:
    """Stash accepted-but-not-honored request fields under a reserved key that
    ``unpack_dynamic_vars`` strips, so they persist for audit WITHOUT polluting the
    echoed ``metadata`` / ``retell_llm_dynamic_variables``. Mirrors the ``__meta__``
    mechanism; shares the ``__meta`` prefix that client keys are already barred from."""
    extras = {
        key: value
        for key, value in (
            ("agent_override", agent_override),
            ("current_node_id", current_node_id),
            ("current_state", current_state),
        )
        if value is not None
    }
    if not extras:
        return packed
    return {**packed, _UNHONORED_KEY: json.dumps(extras)}
```

  Then in `unpack_dynamic_vars`, drop the reserved audit key (add the `rest.pop` line):

```python
def unpack_dynamic_vars(
    stored: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inverse of ``pack_dynamic_vars``: split a stored ``dynamic_vars`` dict back into
    ``(retell_llm_dynamic_variables, metadata)`` with original metadata types preserved.
    The reserved un-honored audit blob is dropped (never echoed)."""
    rest = dict(stored or {})
    raw = rest.pop(_META_KEY, None)
    rest.pop(_UNHONORED_KEY, None)
    metadata: dict[str, Any] = json.loads(raw) if isinstance(raw, str) and raw else {}
    return rest, metadata
```

- [ ] **Step 4: Extend `CompatCall`** — in `apps/api/src/usan_api/compat/schemas/calls.py`,
  in `CompatCall` (around lines 125-153): make `direction` optional and add
  `access_token`. Change `direction: str` → `direction: str | None = None`, and add
  after the `call_type` line:

```python
    access_token: str | None = None  # web calls only; minted on serialize, never stored
```

  (Keep `call_type: str = "phone_call"` — a plain `str` field accepts `"web_call"`; the
  web branch sets it explicitly. No discriminator/union validation applies.)

- [ ] **Step 5: Add the web branch to `serialize_call`** — in
  `apps/api/src/usan_api/compat/call_serializer.py`. Add imports at the top
  (`CallType` onto the base import; `mint_browser_token`):

```python
from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.livekit_dispatch import mint_browser_token
```

  Then in `serialize_call`, just before the `return CompatCall(...)`, mint the token and
  null the phone fields for a web call:

```python
    is_web = call.call_type is CallType.WEB_CALL
    access_token: str | None = None
    if is_web:
        # A web call's browser join token: minted on demand, scoped to this room,
        # never persisted, never logged. Phone-only fields are omitted (V2WebCallResponse).
        access_token = mint_browser_token(
            settings, room=call.livekit_room or "", identity=ids.encode_call_id(call.id)
        )
        from_number = to_number = None
```

  and change the affected fields inside the `CompatCall(...)` construction:

```python
        call_type="web_call" if is_web else "phone_call",
        ...
        from_number=from_number,
        to_number=to_number,
        direction=None if is_web else call.direction.value,
        telephony_identifier=(
            None
            if is_web
            else ({"twilio_call_sid": call.sip_call_id} if call.sip_call_id else None)
        ),
        access_token=access_token,
        ...
```

  (Leave every other field as-is; for a `REGISTERED` web call the transcript/recording/
  metrics lookups simply return empty and those fields are omitted by `exclude_none`.)

- [ ] **Step 6: Run the test**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_web_serializer.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Regression — phone serialization unchanged**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_calls.py -q`
Expected: PASS (no phone regressions — `call_type` default + `access_token`/`direction`
optional are additive).

- [ ] **Step 8: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/serialization.py \
  apps/api/src/usan_api/compat/schemas/calls.py \
  apps/api/src/usan_api/compat/call_serializer.py \
  apps/api/tests/compat/test_web_serializer.py
git commit -m "feat(api): serialize web calls (mint access_token, omit phone fields)"
```

---

### Task 3: `CreateWebCallRequest` schema

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py`
- Test: `apps/api/tests/compat/test_web_request_schema.py`

**Interfaces:**
- Produces: `CreateWebCallRequest` with `agent_id` (required) + the optional fields per
  the oracle, `extra="forbid"`.

- [ ] **Step 1: Write the failing test** — `apps/api/tests/compat/test_web_request_schema.py`

```python
"""CreateWebCallRequest: required agent_id, accepts the heavier optional fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.calls import CreateWebCallRequest


def test_minimal_request_requires_only_agent_id() -> None:
    req = CreateWebCallRequest(agent_id="agent_abc")
    assert req.agent_id == "agent_abc"
    assert req.agent_override is None


def test_accepts_heavier_optional_fields() -> None:
    req = CreateWebCallRequest(
        agent_id="agent_abc",
        agent_version="latest",
        agent_override={"voice_id": "v"},
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
        current_node_id="n1",
        current_state="s1",
    )
    assert req.agent_override == {"voice_id": "v"}
    assert req.current_node_id == "n1"


def test_missing_agent_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest()


def test_empty_agent_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest(agent_id="")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest(agent_id="agent_abc", bogus="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_web_request_schema.py -q`
Expected: FAIL — `ImportError: cannot import name 'CreateWebCallRequest'`.

- [ ] **Step 3: Add the schema** — in `apps/api/src/usan_api/compat/schemas/calls.py`,
  after `RegisterPhoneCallRequest`:

```python
class CreateWebCallRequest(BaseModel):
    """POST /v2/create-web-call. Oracle: agent_id required; the rest optional.

    ``agent_override`` / ``current_node_id`` / ``current_state`` are accepted (zero-change
    repoint) and persisted for audit (compat.serialization.pack_unhonored), but NOT
    honored — there is no conformant field to echo them in. See design §2.2.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    agent_version: int | str | None = None
    agent_override: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, str] | None = None
    current_node_id: str | None = None
    current_state: str | None = None
```

- [ ] **Step 4: Run the test**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_web_request_schema.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/schemas/calls.py \
  apps/api/tests/compat/test_web_request_schema.py
git commit -m "feat(api): add CreateWebCallRequest compat schema"
```

---

### Task 4: `dispatch_web_agent` + `create_web_call` service

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py`
- Modify: `apps/api/src/usan_api/compat/call_create.py`
- Test: `apps/api/tests/test_dispatch_web_agent.py`
- Test: `apps/api/tests/compat/test_create_web_call_service.py`

**Interfaces:**
- Consumes: `CallType`, `CreateWebCallRequest`, `pack_dynamic_vars`, `pack_unhonored`,
  `RESERVED_VAR_PREFIX`, `decode_agent_id`, `agent_profiles_repo.is_live_profile`.
- Produces:
  `dispatch_web_agent(*, settings, room, call_id, dynamic_vars, resolved_vars, timezone) -> None`
  (creates room, dispatches with `session_kind="call"`, `call_type="web_call"`);
  `create_web_call(db, settings, body) -> Call`.

- [ ] **Step 1: Write the failing dispatch test** — `apps/api/tests/test_dispatch_web_agent.py`

```python
"""dispatch_web_agent: web dispatch metadata carries session_kind=call + call_type=web_call,
creates the room, and does NOT require outbound (SIP) configuration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from usan_api import livekit_dispatch


@pytest.mark.asyncio
async def test_dispatch_web_agent_metadata_and_room(settings) -> None:
    fake_api = MagicMock()
    fake_api.__aenter__ = AsyncMock(return_value=fake_api)
    fake_api.__aexit__ = AsyncMock(return_value=False)
    fake_api.room.create_room = AsyncMock()
    fake_api.agent_dispatch.create_dispatch = AsyncMock()

    with patch.object(livekit_dispatch, "build_livekit_api", return_value=fake_api):
        await livekit_dispatch.dispatch_web_agent(
            settings=settings,
            room="usan-web-xyz",
            call_id="cid-1",
            dynamic_vars={"name": "Pat"},
            resolved_vars={},
            timezone="America/New_York",
        )

    fake_api.room.create_room.assert_awaited_once()
    dispatch_arg = fake_api.agent_dispatch.create_dispatch.await_args.args[0]
    meta = json.loads(dispatch_arg.metadata)
    assert meta["session_kind"] == "call"
    assert meta["call_type"] == "web_call"
    assert meta["call_id"] == "cid-1"
    assert meta["dynamic_vars"] == {"name": "Pat"}
    assert meta["resolved_vars"] == {}
```

- [ ] **Step 2: Run it (fails)**

Run: `cd apps/api && uv run pytest -n0 tests/test_dispatch_web_agent.py -q`
Expected: FAIL — `AttributeError: module 'usan_api.livekit_dispatch' has no attribute 'dispatch_web_agent'`.

- [ ] **Step 3: Add `dispatch_web_agent`** — in `apps/api/src/usan_api/livekit_dispatch.py`,
  after `dispatch_test_agent`:

```python
def _web_metadata(
    *,
    call_id: str,
    dynamic_vars: dict[str, Any],
    resolved_vars: dict[str, str],
    timezone: str,
) -> str:
    """Dispatch metadata for a live web call (session_kind=call, call_type=web_call).

    The worker routes on ``call_type == "web_call"`` to its web branch (no SIP read, no
    voicemail). ``dynamic_vars`` is the bare operator/CRM var map (the reserved metadata /
    un-honored blobs are NOT sent to the agent)."""
    return json.dumps(
        {
            "session_kind": "call",
            "call_type": "web_call",
            "call_id": call_id,
            "direction": "inbound",
            "dynamic_vars": dynamic_vars,
            "resolved_vars": resolved_vars,
            "timezone": timezone,
        }
    )


async def dispatch_web_agent(
    *,
    settings: Settings,
    room: str,
    call_id: str,
    dynamic_vars: dict[str, Any],
    resolved_vars: dict[str, str],
    timezone: str,
) -> None:
    """Create the web-call room and dispatch the agent into it (no SIP, no outbound gate).

    Mirrors ``dispatch_test_agent`` but for a real persisted call: the browser joins
    ``room`` over WebRTC with the minted token; the agent answers via its ``_run_web``
    branch. Unlike ``dispatch_agent`` this does NOT require outbound (Telnyx SIP) config —
    a web call places no PSTN leg."""
    async with build_livekit_api(settings) as lkapi:
        try:
            await lkapi.room.create_room(api.CreateRoomRequest(name=room))
        except Exception:  # noqa: BLE001 - room may already exist; dispatch still proceeds
            logger.bind(room=room).debug("create_room for web call was a no-op")
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=room,
                metadata=_web_metadata(
                    call_id=call_id,
                    dynamic_vars=dynamic_vars,
                    resolved_vars=resolved_vars,
                    timezone=timezone,
                ),
            )
        )
    logger.bind(call_id=call_id, room=room).info("Web agent dispatched (session_kind=call)")
```

- [ ] **Step 4: Run the dispatch test (passes)**

Run: `cd apps/api && uv run pytest -n0 tests/test_dispatch_web_agent.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing service test** — `apps/api/tests/compat/test_create_web_call_service.py`

```python
"""create_web_call service: agent gate, REGISTERED web row, dispatch, audit-stash, 502."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from usan_api import livekit_dispatch
from usan_api.compat import call_create
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_agent_id
from usan_api.compat.schemas.calls import CreateWebCallRequest
from usan_api.compat.serialization import unpack_dynamic_vars
from usan_api.db.base import CallStatus, CallType


@pytest.mark.asyncio
async def test_create_web_call_persists_registered_web_row(
    db_session, settings, published_profile
) -> None:
    body = CreateWebCallRequest(
        agent_id=encode_agent_id(published_profile.id),
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
        agent_override={"voice_id": "v"},
    )
    with patch.object(livekit_dispatch, "dispatch_web_agent", new=AsyncMock()) as disp:
        call = await call_create.create_web_call(db_session, settings, body)

    assert call.call_type is CallType.WEB_CALL
    assert call.status is CallStatus.REGISTERED
    assert call.livekit_room and call.livekit_room.startswith("usan-web-")
    # un-honored audit blob persisted but NOT echoed
    dynamic_vars, metadata = unpack_dynamic_vars(call.dynamic_vars)
    assert dynamic_vars == {"name": "Pat"}
    assert metadata == {"external_id": "e1"}
    assert "__meta_unhonored__" in call.dynamic_vars
    # dispatch received only the bare user vars
    disp.assert_awaited_once()
    assert disp.await_args.kwargs["dynamic_vars"] == {"name": "Pat"}


@pytest.mark.asyncio
async def test_create_web_call_rejects_unpublished_agent(db_session, settings) -> None:
    body = CreateWebCallRequest(agent_id="agent_" + "0" * 32)
    with pytest.raises(CompatError) as exc:
        await call_create.create_web_call(db_session, settings, body)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_web_call_dispatch_failure_rolls_back(
    db_session, settings, published_profile
) -> None:
    body = CreateWebCallRequest(agent_id=encode_agent_id(published_profile.id))
    with patch.object(
        livekit_dispatch, "dispatch_web_agent", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        with pytest.raises(CompatError) as exc:
            await call_create.create_web_call(db_session, settings, body)
    assert exc.value.status_code == 502
    assert "boom" not in str(exc.value.message)  # internal detail never surfaced
```

> `published_profile` must be a published `AgentProfile` whose `encode_agent_id(id)` is a
> live agent. Reuse the existing published-agent fixture/helper; if none, seed one as in
> `tests/compat/test_freeze_calls.py`.

- [ ] **Step 6: Run it (fails)**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_web_call_service.py -q`
Expected: FAIL — `AttributeError: module 'usan_api.compat.call_create' has no attribute 'create_web_call'`.

- [ ] **Step 7: Add the service** — in `apps/api/src/usan_api/compat/call_create.py`.
  Add imports at the top (`from loguru import logger`; `from usan_api import livekit_dispatch`;
  `CallType` onto the base import; `pack_unhonored` onto the serialization import; and
  `CreateWebCallRequest` onto the schemas import):

```python
from loguru import logger
from usan_api import livekit_dispatch
from usan_api.compat.schemas.calls import (
    CreatePhoneCallRequest,
    CreateWebCallRequest,
    RegisterPhoneCallRequest,
)
from usan_api.compat.serialization import RESERVED_VAR_PREFIX, pack_dynamic_vars, pack_unhonored
from usan_api.db.base import CallDirection, CallStatus, CallType
```

  Then add the service function:

```python
async def create_web_call(
    db: AsyncSession,
    settings: Settings,
    body: CreateWebCallRequest,
) -> Call:
    """Create + dispatch a live LiveKit web call; returns the REGISTERED web Call row.

    No contact / DNC / quiet-hours (web calls are join-link, not PSTN). The agent is
    resolved + gated exactly like register-phone-call (agent_id required + published).
    """
    profile_id = decode_agent_id(body.agent_id)
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise CompatError(422, "agent_id must reference a published agent")
    if any(
        str(k).startswith(RESERVED_VAR_PREFIX) for k in (body.retell_llm_dynamic_variables or {})
    ):
        raise CompatError(422, "retell_llm_dynamic_variables keys must not start with '__meta'")

    packed = pack_dynamic_vars(body.retell_llm_dynamic_variables, body.metadata)
    packed = pack_unhonored(
        packed,
        agent_override=body.agent_override,
        current_node_id=body.current_node_id,
        current_state=body.current_state,
    )
    room = f"usan-web-{uuid.uuid4().hex}"
    call = Call(
        call_type=CallType.WEB_CALL,
        status=CallStatus.REGISTERED,
        direction=CallDirection.INBOUND,  # internal placeholder; call_type is authoritative
        profile_override=profile_id,
        dynamic_vars=packed,
        livekit_room=room,
        contact_id=None,
    )
    db.add(call)
    await db.flush()

    try:
        await livekit_dispatch.dispatch_web_agent(
            settings=settings,
            room=room,
            call_id=str(call.id),
            dynamic_vars=body.retell_llm_dynamic_variables or {},
            resolved_vars={},
            timezone=settings.compat_default_timezone,
        )
    except Exception as exc:
        # PHI/secret-safe: type name only — never str(exc), metadata, or any token.
        await db.rollback()
        logger.bind(err=type(exc).__name__).error("web call dispatch failed")
        raise CompatError(502, "web call dispatch failed") from None

    await db.commit()
    await db.refresh(call)
    return call
```

- [ ] **Step 8: Run the service test (passes)**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_create_web_call_service.py -q`
Expected: PASS (3 passed).

- [ ] **Step 9: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add apps/api/src/usan_api/livekit_dispatch.py apps/api/src/usan_api/compat/call_create.py \
  apps/api/tests/test_dispatch_web_agent.py apps/api/tests/compat/test_create_web_call_service.py
git commit -m "feat(api): add dispatch_web_agent + create_web_call service"
```

---

### Task 5: route + surface coverage + conformance freeze test

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/calls.py`
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py`
- Modify: `apps/api/tests/test_compat_fidelity.py`
- Modify: `apps/api/tests/compat/conftest.py`
- Test: `apps/api/tests/compat/test_freeze_web_calls.py`

**Interfaces:**
- Consumes: `create_web_call`, `CreateWebCallRequest`, `serialize_call`.
- Produces: served `POST /v2/create-web-call`; `_UNSUPPORTED` no longer lists it.

- [ ] **Step 1: Write the failing freeze test** — `apps/api/tests/compat/test_freeze_web_calls.py`

```python
"""Contract freeze for POST /v2/create-web-call (RetellAI parity Phase 3)."""

from __future__ import annotations

from .conformance import assert_conforms, assert_sdk_roundtrip


def _create_web_call(client, headers, agent_id, **overrides):
    body = {"agent_id": agent_id}
    body.update(overrides)
    return client.post("/v2/create-web-call", json=body, headers=headers)


def test_create_web_call_requires_key(compat_client, web_agent_id, mock_web_dispatch):
    r = compat_client.post("/v2/create-web-call", json={"agent_id": web_agent_id})
    assert r.status_code == 401


def test_create_web_call_conforms(compat_client, compat_headers, web_agent_id, mock_web_dispatch):
    r = _create_web_call(compat_client, compat_headers, web_agent_id,
                         metadata={"external_id": "e1"},
                         retell_llm_dynamic_variables={"name": "Pat"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["call_type"] == "web_call"
    assert body["access_token"]
    assert body["call_status"] == "registered"
    for phone_field in ("from_number", "to_number", "direction", "telephony_identifier"):
        assert phone_field not in body
    assert_conforms(body, "V2WebCallResponse")
    assert_sdk_roundtrip(body, "retell.types:WebCallResponse")


def test_create_web_call_rejects_malformed_agent_id(compat_client, compat_headers, mock_web_dispatch):
    r = _create_web_call(compat_client, compat_headers, "not-an-agent-id")
    assert r.status_code == 422


def test_create_web_call_rejects_unpublished_agent(compat_client, compat_headers, mock_web_dispatch):
    r = _create_web_call(compat_client, compat_headers, "agent_" + "0" * 32)
    assert r.status_code == 422


def test_heavier_optional_fields_accepted(compat_client, compat_headers, web_agent_id, mock_web_dispatch):
    r = _create_web_call(compat_client, compat_headers, web_agent_id,
                         agent_override={"voice_id": "v"}, current_node_id="n1", current_state="s1")
    assert r.status_code == 201, r.text


def test_metadata_round_trips_on_get(compat_client, compat_headers, web_agent_id, mock_web_dispatch):
    created = _create_web_call(compat_client, compat_headers, web_agent_id,
                              metadata={"external_id": "e1"},
                              retell_llm_dynamic_variables={"name": "Pat"},
                              agent_override={"voice_id": "v"}).json()
    got = compat_client.get(f"/v2/get-call/{created['call_id']}", headers=compat_headers).json()
    assert got["call_type"] == "web_call"
    assert got["metadata"] == {"external_id": "e1"}
    assert got["retell_llm_dynamic_variables"] == {"name": "Pat"}
    # the accepted-but-not-honored fields never leak into the echo
    assert "agent_override" not in got["metadata"]
    assert "__meta_unhonored__" not in got["retell_llm_dynamic_variables"]
    assert_conforms(got, "V2WebCallResponse")
```

- [ ] **Step 2: Add the `mock_web_dispatch` + `web_agent_id` fixtures** — in
  `apps/api/tests/compat/conftest.py`:

```python
@pytest.fixture
def web_agent_id(compat_client, compat_headers) -> str:
    """A published agent_id usable as create-web-call's agent_id."""
    return _published_agent_id(compat_client, compat_headers)


@pytest.fixture
def mock_web_dispatch(monkeypatch):
    """Stub the LiveKit web dispatch so the freeze tests place no real call."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("usan_api.livekit_dispatch.dispatch_web_agent", AsyncMock())
```

> `mint_browser_token` is NOT mocked — it produces a real JWT locally; the test asserts a
> non-empty `access_token`. It needs a valid `LIVEKIT_API_SECRET` (≥32 chars) in the test
> settings; the compat test config already provides LiveKit settings (the test-audio path
> uses them). If a `KeyError`/validation error appears, add the LiveKit test settings the
> same way the existing LiveKit-touching tests do.

- [ ] **Step 3: Run it (fails)**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_web_calls.py -q`
Expected: FAIL — the route 501s (still in `_UNSUPPORTED`), so the `status_code == 201`
assertions fail.

- [ ] **Step 4: Remove from `_UNSUPPORTED`** — in
  `apps/api/src/usan_api/compat/routers/unsupported.py`, delete the `# --- Web call ---`
  comment line and the `("POST", "/v2/create-web-call"),` tuple beneath it.

- [ ] **Step 5: Remove from the fidelity 501 list** — in
  `apps/api/tests/test_compat_fidelity.py`, delete the `("post", "/v2/create-web-call"),`
  line from the `test_out_of_scope_returns_501_envelope` parametrize list.

- [ ] **Step 6: Add the route** — in `apps/api/src/usan_api/compat/routers/calls.py`:
  add `CreateWebCallRequest` to the `from usan_api.compat.schemas.calls import (...)`
  block, then add the handler after `create_phone_call`:

```python
@router.post(
    "/v2/create-web-call",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatCall,
    response_model_exclude_none=True,
)
async def create_web_call(
    body: CreateWebCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatCall:
    call = await call_create.create_web_call(db, settings, body)
    _audit(request, "create-web-call", ids.encode_call_id(call.id))
    return await call_serializer.serialize_call(db, call, settings, client_host=client_ip(request))
```

- [ ] **Step 7: Run the freeze test + the surface gates**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_web_calls.py tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -q`
Expected: PASS. `test_surface_coverage` derives served paths from the live route table,
so it goes green automatically; `test_compat_fidelity` goes green once the parametrize
line is removed. **Both must pass** (the Phase 2 two-files lesson — do not run only
`tests/compat`).

- [ ] **Step 8: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add apps/api/src/usan_api/compat/routers/calls.py \
  apps/api/src/usan_api/compat/routers/unsupported.py \
  apps/api/tests/test_compat_fidelity.py apps/api/tests/compat/conftest.py \
  apps/api/tests/compat/test_freeze_web_calls.py
git commit -m "feat(api): serve POST /v2/create-web-call (compat web calls)"
```

---

### Task 6: agent worker `_run_web` branch

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_web_session.py`

**Interfaces:**
- Consumes: `build_check_in_agent`, `CheckInData`, `build_session`,
  `register_transcript_flush`, `register_metrics_flush`, `_register_dynamic_vars_receiver`,
  `_arm_crisis_safety_net`, `_max_duration_guard`, `say_recording_disclosure`,
  `start_call_recording`, `greet`, `validate_call_id`, `prompt_vars.build_vars`,
  `sms_template_instructions` (all already imported in `worker.py`).
- Produces: `CallMetadata.call_type`; `entrypoint` routes `web_call` to `_run_web`.

> **Read `worker.py` in full first.** The outbound logic is an inline block inside
> `entrypoint` (around lines 420-496), not a function. `_run_web` replicates the
> session/agent setup + side-effects of that block, KEEPS a generic `wait_for_participant`
> (a browser join IS a participant to await — do NOT skip it), and SKIPS the voicemail
> pieces. After recording starts it greets and lets the session run (mirror the inbound
> known-contact path, which greets and returns while the session continues).

- [ ] **Step 1: Write the failing test** — `services/agent/tests/test_web_session.py`

> Model this on `services/agent/tests/test_test_session.py` (the existing browser-
> participant test). Read it first and reuse its harness (the fake `JobContext`, the
> participant with empty `attributes`, the assertion style).

```python
"""_run_web: browser participant, no SIP read, no voicemail, full side-effects."""

from __future__ import annotations

from usan_agent.worker import CallMetadata, parse_metadata


def test_call_metadata_parses_call_type() -> None:
    meta = parse_metadata('{"session_kind": "call", "call_type": "web_call", "call_id": "c1"}')
    assert meta.call_type == "web_call"
    assert meta.session_kind == "call"


def test_call_metadata_defaults_call_type_to_phone() -> None:
    # An existing outbound/inbound dispatch (no call_type key) stays phone_call.
    meta = parse_metadata('{"direction": "outbound", "call_id": "c1"}')
    assert meta.call_type == "phone_call"
    assert isinstance(meta, CallMetadata)
```

> Add behavioral `_run_web` tests mirroring `test_test_session.py`: assert that for a
> `call_type="web_call"` job the worker (a) never reads `sip.*` attributes, (b) does not
> construct a `VoicemailWatcher` / call `_run_detection_window`, and (c) registers the
> transcript + metrics flushes and starts recording. Use the same monkeypatch/spy approach
> `test_test_session.py` uses for `start_call_recording`, `register_transcript_flush`, etc.

- [ ] **Step 2: Run it (fails)**

Run: `cd services/agent && uv run pytest tests/test_web_session.py -q`
Expected: FAIL — `AttributeError: 'CallMetadata' object has no attribute 'call_type'`.

- [ ] **Step 3: Add `call_type` to `CallMetadata` + `parse_metadata`** — in
  `services/agent/src/usan_agent/worker.py`. In the `CallMetadata` dataclass add (after
  `test_config`):

```python
    # Transport discriminator. Absent on every phone dispatch → "phone_call". "web_call"
    # selects the WebRTC branch (_run_web): generic participant, no SIP, no voicemail.
    call_type: str = "phone_call"
```

  In `parse_metadata`, add to the `CallMetadata(...)` constructor:

```python
        call_type=data.get("call_type") or "phone_call",
```

- [ ] **Step 4: Add `_run_web` + route it** — in `worker.py`, add the function (place it
  near `_run_inbound`):

```python
async def _run_web(ctx: JobContext, settings: Settings, cfg: AgentConfig, meta: CallMetadata, log: Any) -> None:
    """Live web (WebRTC) call: a browser participant, full side-effects, no SIP, no voicemail.

    Structurally the outbound block minus the SIP/voicemail pieces. The browser joins the
    room with the API-minted token; the agent greets and runs the configured check-in.
    """
    try:
        call_id = validate_call_id(meta.call_id)
    except ValueError:
        log.error("Invalid call_id in web job metadata; refusing job")
        ctx.shutdown(reason="invalid_metadata")
        return
    data = CheckInData(
        call_id=call_id, settings=settings, job_ctx=ctx, goodbye_message=cfg.prompts.goodbye_message
    )
    session = build_session(settings, cfg, userdata=data)
    agent = build_check_in_agent(
        cfg, resolved_vars=meta.resolved_vars, custom_vars=meta.dynamic_vars, timezone=meta.timezone
    )
    _web_vars = prompt_vars.build_vars(
        meta.resolved_vars or {}, meta.dynamic_vars or {}, timezone=meta.timezone, now=datetime.now(UTC)
    )
    _register_dynamic_vars_receiver(
        ctx, agent, _web_vars, cfg.prompts.checkin_flow_instructions + sms_template_instructions(cfg.tools)
    )
    register_transcript_flush(ctx, session, call_id, settings)
    register_metrics_flush(ctx, session, call_id, settings)
    await session.start(agent=agent, room=ctx.room)
    log.info("Web session started; waiting for browser participant")
    try:
        await asyncio.wait_for(ctx.wait_for_participant(), timeout=cfg.timing.answer_timeout_s)
    except TimeoutError:
        log.info("No web participant within answer timeout; ending job")
        ctx.shutdown(reason="no_answer_timeout")
        return
    _guard_task = asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
    _BACKGROUND_TASKS.add(_guard_task)
    _guard_task.add_done_callback(_BACKGROUND_TASKS.discard)
    await say_recording_disclosure(session, cfg)
    await start_call_recording(ctx, call_id, settings)
    _arm_crisis_safety_net(session, call_id=call_id, settings=settings)
    # No voicemail detection for a browser participant — greet and run the conversation.
    await greet(session, cfg, include_disclosure=False)
```

  Then route it in `entrypoint`, immediately after `cfg` is resolved and BEFORE the
  `if meta.direction == "outbound" ...` block (and after the `session_kind == "test"`
  branch):

```python
    if meta.call_type == "web_call":
        await _run_web(ctx, settings, cfg, meta, log)
        return
```

> `greet` is the helper used in `_run_detection_window` (`await greet(session, cfg,
> include_disclosure=False)`). If `_run_inbound`'s known-contact path drives the
> conversation differently (e.g. `session.generate_reply(...)`), match THAT — read
> `_run_inbound` and reuse its post-greet pattern so the live conversation behaves
> identically to a phone check-in.

- [ ] **Step 5: Run the worker tests (pass)**

Run: `cd services/agent && uv run pytest tests/test_web_session.py -q`
Expected: PASS.

- [ ] **Step 6: Regression — test-session + worker tests still pass**

Run: `cd services/agent && uv run pytest tests/test_test_session.py tests/test_worker.py -q`
Expected: PASS (routing change is additive; phone/test paths unchanged).

- [ ] **Step 7: Lint + type-check + commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format . && uv run mypy
git add services/agent/src/usan_agent/worker.py services/agent/tests/test_web_session.py
git commit -m "feat(agent): add _run_web branch for live web calls"
```

---

### Task 7: deployment docs note (browser-interop caveat)

**Files:**
- Create: `docs/deployment/web-calls-livekit-url.md`

- [ ] **Step 1: Write the doc** — `docs/deployment/web-calls-livekit-url.md`

```markdown
# Web Calls — LiveKit server URL (client-side integration)

Phase 3 serves `POST /v2/create-web-call` (RetellAI-compatible). The response carries a
real, working LiveKit WebRTC `access_token`, and our agent answers the browser
participant. The REST contract is fully oracle-conformant.

## The one caveat: the token does not carry a server URL

The minted `access_token` encodes the **room** and **participant identity**, not the
LiveKit **server URL**. RetellAI's `RetellWebClient` connects to *RetellAI's* LiveKit
cloud by default. To join a call created against this engine, the client's frontend must
connect to **our** `LIVEKIT_URL` (the `wss://…` the deployment exposes) using the minted
token — e.g. raw `livekit-client`:

    import { Room } from "livekit-client";
    const room = new Room();
    await room.connect(USAN_LIVEKIT_WSS_URL, accessToken);

A true zero-change repoint of a RetellAI **browser** SDK that hardcodes the RetellAI
server URL is therefore not possible from the token alone — the client must point at our
`LIVEKIT_URL`. (Same class of edge-integration caveat as outbound webhook delivery: the
REST surface is drop-in; one client-side endpoint must target our infrastructure.)

## Operator checklist

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` must be set (already required by
  the existing outbound/test-audio paths — no new env keys).
- Migration `0041` (the `call_type` column) must be applied — it ships with the `v*` tag
  deploy and runs as the `usan` table owner.
- No master enable flag: like the rest of the compat surface, `create-web-call` 401s
  until a super-admin mints a compat key.
```

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/web-calls-livekit-url.md
git commit -m "docs: web-calls LiveKit server-URL integration note"
```

---

## Final gate (subagent-driven-development process step, not a numbered task)

After Task 7, run the whole-branch review (most-capable model) and the full suites:

```bash
cd apps/api && uv run pytest -q          # full api suite (parallel) — both 501 files included
cd services/agent && uv run pytest -q    # full agent suite
```

Then finish the branch (squash-merge to `main`). **No `v*` tag.**

---

## Self-Review

**Spec coverage:** §3 request/response → Tasks 2,3,5; §4 data model → Task 1; §5 serializer
→ Task 2; §6 schema → Task 3; §7 service + `dispatch_web_agent` → Task 4; §8 worker → Task
6; §9 conformance/surface → Tasks 2,5,6; §11 docs caveat → Task 7. All covered.

**Placeholder scan:** every code step contains real code; tests have real assertions. The
only intentional "read the file" directives are for `worker.py`'s inline outbound block
and `test_test_session.py` (the implementer must match the live-conversation pattern) —
the exact functions to call are named, with verbatim signatures from the spec/grounding.

**Type consistency:** `CallType` (base.py) → `Call.call_type` (models.py) → `serialize_call`
branch + `create_web_call` (`CallType.WEB_CALL`); `CreateWebCallRequest` (Task 3) consumed
by `create_web_call` (Task 4) + the route (Task 5); `dispatch_web_agent` signature
identical across Task 4's definition, its test, and the service call; `pack_unhonored` /
`_UNHONORED_KEY` consistent across serialization.py + service + serializer tests.
