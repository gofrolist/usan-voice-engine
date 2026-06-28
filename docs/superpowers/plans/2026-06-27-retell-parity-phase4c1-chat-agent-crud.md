# Phase 4c-1 — Chat-Agent CRUD — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the 7 RetellAI `*-chat-agent` operations by overlaying `agent_profiles` with a new `channel` discriminator, moving them from documented-501 to served while keeping `KNOWN_GAPS` empty.

**Architecture:** A chat agent IS an `agent_profiles` row marked `channel='chat'` (reusing the `agent_<hex>` id space, versioning, RLS, id-codec — exactly as voice agents work). A new `channel` column (migration 0045, `NOT NULL DEFAULT 'voice'`) discriminates voice vs chat; every voice/admin/call-plane reader is filtered/guarded so a chat row can never be listed-as-voice, dialed, defaulted, assigned, or used as an override (the Phase-3 leak lesson). Retell-LLM ops stay channel-agnostic (an LLM is shared infra). New compat schemas/bridge/router serve the 7 ops; the chat bridge reuses `agent_bridge`'s overlay helpers.

**Tech Stack:** FastAPI, SQLAlchemy 2.x async, Alembic, Pydantic v2, pytest (`-n auto`), `retell-sdk==5.53.0`, the pinned oracle conformance harness.

**Spec:** `docs/superpowers/specs/2026-06-27-retell-parity-phase4c1-chat-agent-crud-design.md`

## Global Constraints

- Commit `feat(api): …`; scope `api`. Squash-merge to protected `main` **only on explicit go-ahead**. **No `v*` tag.** Attribution disabled (no `Co-Authored-By` / footer).
- `apps/api` and `services/agent` never import each other.
- Migration `0045` is additive owner-DDL (runs as `usan` owner on deploy), inert; single alembic head after = `0045`. Channel column `NOT NULL DEFAULT 'voice'`.
- Channel values are the string literals `'voice'` / `'chat'`. New repo/bridge params default to `None` = channel-agnostic, so retell-llm ops stay untouched; **only agent-typed callers pass `'voice'`**.
- PHI/secret-safe logging only (`_audit` = org id + op + agent id; never config/prompt text). `organization_id` is server-set by RLS, never by app code.
- `exclude_none` discipline on every serialized response. The 7 served paths use the oracle's exact path strings. `KNOWN_GAPS` stays `frozenset()`.
- Run before pushing: `ruff check . && ruff format --check .`, `uv run mypy` (config `files=["src"]`; **never** `mypy .`), `uv run pytest`.
- Commands run from `apps/api/`. Tests use `compat_client`/`compat_headers` (compat-key Bearer → RLS org) or `app_session` + `set_tenant_context` for repo-level tests; seed RLS rows via the superuser engine inside the test body.

---

### Task 1: Migration 0045 + ORM `AgentProfile.channel`

**Files:**
- Create: `apps/api/migrations/versions/0045_agent_channel.py`
- Modify: `apps/api/src/usan_api/db/models.py` (AgentProfile, after the `status` column ~496)
- Test: `apps/api/tests/test_agent_channel_column.py`

**Interfaces:**
- Produces: `AgentProfile.channel: Mapped[str]` (`'voice'`|`'chat'`, server_default `'voice'`); alembic head `0045`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_agent_channel_column.py
"""Phase 4c-1: agent_profiles.channel defaults to 'voice' (migration 0045 backfill)."""

from __future__ import annotations

import pytest

from usan_api.repositories import agent_profiles as repo
from usan_api.tenancy import set_tenant_context


@pytest.mark.asyncio
async def test_new_profile_defaults_channel_voice(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    profile = await repo.create_profile(
        app_session, name="chan-default", description=None, actor_email="t@example.com"
    )
    assert profile.channel == "voice"
```

If the `app_session` / `seeded_org_id` fixtures differ, mirror an existing repo-level test in `apps/api/tests/` (e.g. a test that calls `set_tenant_context` then a repo function) for the exact fixture names and the tenancy import path.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -n0 tests/test_agent_channel_column.py -q`
Expected: FAIL — `AttributeError: 'AgentProfile' object has no attribute 'channel'` (or an `UndefinedColumn` from the DB).

- [ ] **Step 3: Add the migration**

```python
# apps/api/migrations/versions/0045_agent_channel.py
"""agent_profiles: add channel discriminator (voice|chat) for the chat-agent overlay (Phase 4c-1).

Additive. Distinguishes voice agents (channel='voice' — the default/backfill) from chat agents
(channel='chat', created via the compat create-chat-agent path). The new column inherits the
table's existing usan_app GRANT + RLS policy, so no new grant/policy is needed. Owner-DDL: the
deploy migrates as the usan owner. Inert until a v* tag.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'voice'")),
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "channel")
```

- [ ] **Step 4: Add the ORM column**

In `apps/api/src/usan_api/db/models.py`, inside `class AgentProfile`, immediately after the `status` `mapped_column(...)` block (before `draft_config`), add:

```python
    # Discriminates voice agents (default) from chat agents (Phase 4c-1). A chat agent is an
    # agent_profiles row with channel='chat'; voice/admin/call-plane readers filter to 'voice'
    # so chat rows never leak into the voice surfaces or the call plane.
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'voice'"))
```

`Text` and `text` are already imported in `models.py` (used by other columns).

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest -n0 tests/test_agent_channel_column.py -q`
Expected: PASS.

- [ ] **Step 6: Verify single alembic head**

Run: `uv run alembic heads`
Expected: a single head `0045 (head)`.

- [ ] **Step 7: Commit**

```bash
git add apps/api/migrations/versions/0045_agent_channel.py apps/api/src/usan_api/db/models.py apps/api/tests/test_agent_channel_column.py
git commit -m "feat(api): add agent_profiles.channel discriminator (migration 0045) for chat-agent overlay"
```

---

### Task 2: Repo channel API + call-plane guards

**Files:**
- Modify: `apps/api/src/usan_api/repositories/agent_profiles.py` (`list_profiles` 72-74, `get_default_profile` 375-391, `get_default_holder` 394-410, `_resolved_from_profile` 422-450, `set_default` 256-286, `is_live_profile` 570-578)
- Test: `apps/api/tests/test_agent_channel_repo_guards.py`

**Interfaces:**
- Consumes: `AgentProfile.channel` (Task 1).
- Produces:
  - `list_profiles(db, *, channel: Literal["voice","chat"] | None = None) -> list[AgentProfile]`
  - `is_live_profile(db, profile_id, *, channel: Literal["voice","chat"] | None = None) -> bool`
  - `get_default_profile` / `get_default_holder` now filter `channel == 'voice'`.
  - `_resolved_from_profile` returns `None` for a non-voice profile.
  - `set_default` raises `ProfileInUseError` for a non-voice profile.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_agent_channel_repo_guards.py
"""Phase 4c-1: repo channel filters/guards keep chat rows out of the voice call plane."""

from __future__ import annotations

import pytest

from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.tenancy import set_tenant_context


async def _make_published(session, *, channel: str, name: str) -> "object":
    profile = await repo.create_profile(
        session, name=name, description=None, actor_email="t@example.com"
    )
    profile.channel = channel
    await session.flush()
    await repo.publish(session, profile.id, note="seed", actor_email="t@example.com")
    await session.flush()
    return profile


@pytest.mark.asyncio
async def test_list_profiles_channel_filter(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    voice = await _make_published(app_session, channel="voice", name="v1")
    chat = await _make_published(app_session, channel="chat", name="c1")
    voice_only = {p.id for p in await repo.list_profiles(app_session, channel="voice")}
    assert voice.id in voice_only
    assert chat.id not in voice_only
    all_rows = {p.id for p in await repo.list_profiles(app_session)}
    assert voice.id in all_rows and chat.id in all_rows  # no channel = both


@pytest.mark.asyncio
async def test_is_live_profile_channel(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _make_published(app_session, channel="chat", name="c2")
    assert await repo.is_live_profile(app_session, chat.id) is True  # agnostic
    assert await repo.is_live_profile(app_session, chat.id, channel="voice") is False
    assert await repo.is_live_profile(app_session, chat.id, channel="chat") is True


@pytest.mark.asyncio
async def test_set_default_rejects_chat(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _make_published(app_session, channel="chat", name="c3")
    with pytest.raises(ProfileInUseError):
        await repo.set_default(app_session, chat.id, direction="outbound")


@pytest.mark.asyncio
async def test_get_default_profile_ignores_chat(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _make_published(app_session, channel="chat", name="c4")
    # Force the chat row to hold the default flag (bypassing set_default's guard) to prove the
    # READ filter also excludes it.
    chat.is_default_outbound = True
    await app_session.flush()
    assert await repo.get_default_profile(app_session, "outbound") is None


@pytest.mark.asyncio
async def test_resolved_from_profile_skips_chat(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _make_published(app_session, channel="chat", name="c5")
    profile = await repo.get_profile(app_session, chat.id)
    assert await repo._resolved_from_profile(app_session, profile) is None
```

Use the same fixture names (`app_session`, `seeded_org_id`) as Task 1's test (mirror an existing repo-level test for exact names).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -n0 tests/test_agent_channel_repo_guards.py -q`
Expected: FAIL (channel kwarg unknown / chat rows not filtered / set_default does not raise).

- [ ] **Step 3: Edit `repositories/agent_profiles.py`**

`list_profiles` (replace 72-74):

```python
async def list_profiles(
    db: AsyncSession, *, channel: Literal["voice", "chat"] | None = None
) -> list[AgentProfile]:
    stmt = select(AgentProfile).order_by(AgentProfile.name)
    if channel is not None:
        stmt = stmt.where(AgentProfile.channel == channel)
    result = await db.execute(stmt)
    return list(result.scalars().all())
```

`set_default` — after the ARCHIVED check (insert after the `raise ProfileInUseError("cannot set an archived profile as default")` line, ~266):

```python
    if profile.channel != "voice":
        raise ProfileInUseError("cannot set a chat agent as a call default")
```

`get_default_profile` (replace the `select(...)` at 388-390):

```python
    result = await db.execute(
        select(AgentProfile).where(
            column.is_(True),
            AgentProfile.status == ProfileStatus.ACTIVE,
            AgentProfile.channel == "voice",
        )
    )
```

`get_default_holder` (replace the `select(...)` at 409):

```python
    result = await db.execute(
        select(AgentProfile).where(column.is_(True), AgentProfile.channel == "voice")
    )
```

`_resolved_from_profile` (replace the guard at 430):

```python
    if profile is None or profile.status != ProfileStatus.ACTIVE or profile.channel != "voice":
        return None
```

`is_live_profile` (replace 570-578):

```python
async def is_live_profile(
    db: AsyncSession, profile_id: uuid.UUID, *, channel: Literal["voice", "chat"] | None = None
) -> bool:
    """True iff the profile exists, is ACTIVE, has a published version (the precondition for
    profile_override to take effect, spec §4) and — when ``channel`` is given — matches it, so a
    chat agent passed as a voice override is rejected."""
    profile = await get_profile(db, profile_id)
    return (
        profile is not None
        and profile.status is ProfileStatus.ACTIVE
        and profile.published_version is not None
        and (channel is None or profile.channel == channel)
    )
```

`Literal` is already imported (`from typing import Any, Literal`, line 5).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -n0 tests/test_agent_channel_repo_guards.py -q`
Expected: PASS (all 5).

- [ ] **Step 5: Run the existing agent-profile + call suites (regression)**

Run: `uv run pytest tests/test_admin_profiles.py tests/test_outbound_calls.py -q`
Expected: PASS (all existing voice profiles are `channel='voice'`, so the new filters are no-ops for them).

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/repositories/agent_profiles.py apps/api/tests/test_agent_channel_repo_guards.py
git commit -m "feat(api): channel-scope the agent-profile repo (list/is_live/default/resolve/set_default)"
```

---

### Task 3: Thread `channel='voice'` through every voice caller + agent_bridge

**Files:**
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (`_load_active` 124-128 + its AGENT callers; `list_agent_profiles` 325-328; `bind_agent` 190-222)
- Modify: `apps/api/src/usan_api/compat/routers/agents.py` (`list_agents` 85; `list_agents_v2` 201)
- Modify: `apps/api/src/usan_api/routers/admin_profiles.py` (`list_profiles` handler 46)
- Modify: `apps/api/src/usan_api/routers/admin_contacts.py` (the assign handler ~54-78)
- Modify: `apps/api/src/usan_api/services/outbound_calls.py` (`require_live_override` 35)
- Modify: `apps/api/src/usan_api/compat/call_create.py` (148, 158, 213, 255)
- Modify: `apps/api/src/usan_api/compat/batch_create.py` (170)
- Modify: `apps/api/src/usan_api/routers/batches.py` (97)
- Test: `apps/api/tests/test_agent_channel_callers.py`

**Interfaces:**
- Consumes: Task 2's `list_profiles(channel=…)`, `is_live_profile(channel=…)`.
- Produces:
  - `agent_bridge.list_agent_profiles(db, *, channel: str | None = None)`
  - `agent_bridge._load_active(db, profile_id, *, kind, expected_channel: str | None = None)`
  - `bind_agent` stamps `channel='voice'`. Every voice `is_live_profile` call site passes `channel="voice"`. Retell-llm ops unchanged (agnostic).

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_agent_channel_callers.py
"""Phase 4c-1: voice callers reject a chat profile; admin /profiles list excludes chat."""

from __future__ import annotations

import pytest

from usan_api.repositories import agent_profiles as repo
from usan_api.services.outbound_calls import require_live_override
from usan_api.tenancy import set_tenant_context


async def _published_chat(session, name: str) -> "object":
    profile = await repo.create_profile(
        session, name=name, description=None, actor_email="t@example.com"
    )
    profile.channel = "chat"
    await session.flush()
    await repo.publish(session, profile.id, note="seed", actor_email="t@example.com")
    await session.flush()
    return profile


@pytest.mark.asyncio
async def test_require_live_override_rejects_chat(app_session, seeded_org_id):
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _published_chat(app_session, "chat-caller-1")
    with pytest.raises(Exception) as exc:  # HTTPException(422)
        await require_live_override(app_session, chat.id)
    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_admin_profiles_list_excludes_chat(app_session, seeded_org_id):
    """Repo-level proxy for the admin-ui list (the handler calls list_profiles(channel='voice'))."""
    await set_tenant_context(app_session, seeded_org_id)
    chat = await _published_chat(app_session, "chat-caller-2")
    voice_only = {p.id for p in await repo.list_profiles(app_session, channel="voice")}
    assert chat.id not in voice_only
```

If a full HTTP-level admin-list test is preferred, mirror `tests/test_admin_profiles.py` for the native `client` + admin-auth fixtures and assert the chat id is absent from `GET /v1/admin/profiles`; the repo-level assertion above is the minimum that pins the seal.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -n0 tests/test_agent_channel_callers.py -q`
Expected: FAIL (`require_live_override` accepts the chat profile).

- [ ] **Step 3: Edit `compat/agent_bridge.py`**

`_load_active` (replace 124-128):

```python
async def _load_active(
    db: AsyncSession, profile_id: uuid.UUID, *, kind: str, expected_channel: str | None = None
) -> AgentProfile:
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    if profile is None or profile.status == ProfileStatus.ARCHIVED:
        raise CompatError(404, f"{kind} not found")
    # Cross-resource guard: agent_id/llm_id are two views of one UUID, so a chat row's id
    # re-prefixed as agent_<uuid> would otherwise resolve here. Agent ops pass 'voice'; chat
    # ops pass 'chat'; retell-llm ops pass None (an LLM is channel-agnostic shared infra).
    if expected_channel is not None and profile.channel != expected_channel:
        raise CompatError(404, f"{kind} not found")
    return profile
```

Add `expected_channel="voice"` to the AGENT-kind `_load_active` calls — `get_agent_profile` (318), `update_agent` (228), `publish_agent_version` (277), `delete_agent_version` (294), `delete_agent` (306), `list_agent_versions` (335). Example (`get_agent_profile`):

```python
async def get_agent_profile(db: AsyncSession, agent_id: str) -> AgentProfile:
    return await _load_active(db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice")
```

Leave the `kind="response engine"` calls (`bind_agent` 198, `get_llm_profile` 322, `update_response_engine` 256) WITHOUT `expected_channel` (agnostic).

`list_agent_profiles` (replace 325-328):

```python
async def list_agent_profiles(
    db: AsyncSession, *, channel: str | None = None
) -> list[AgentProfile]:
    """The single agent inventory; archived (deleted) are excluded. ``channel`` filters voice vs
    chat (None = all, used by the channel-agnostic retell-llm list)."""
    profiles = await agent_profiles_repo.list_profiles(db, channel=channel)
    return [p for p in profiles if p.status != ProfileStatus.ARCHIVED]
```

`bind_agent` — stamp `channel='voice'` (insert right after the `if updated is None: raise CompatError(404, "response engine not found")` block, ~217, before the `if body.agent_name:` line):

```python
    updated.channel = "voice"  # a bound voice agent is always channel='voice' (re-stamps a re-bind)
```

- [ ] **Step 4: Edit the voice callers**

`compat/routers/agents.py`:
- `list_agents` (85): `profiles = await agent_bridge.list_agent_profiles(db, channel="voice")`
- `list_agents_v2` (201): `profiles = await agent_bridge.list_agent_profiles(db, channel="voice")`

`routers/admin_profiles.py` `list_profiles` handler (46): `profiles = await repo.list_profiles(db, channel="voice")`

`services/outbound_calls.py` `require_live_override` (35): `if not await agent_profiles_repo.is_live_profile(db, profile_id, channel="voice"):`

`compat/call_create.py` — add `, channel="voice"` to the four `is_live_profile(...)` calls (148, 158, 213, 255). Example (148-150):

```python
    if profile_override is not None and not await agent_profiles_repo.is_live_profile(
        db, profile_override, channel="voice"
    ):
```

`compat/batch_create.py` (170): `if not await agent_profiles_repo.is_live_profile(db, override_id, channel="voice"):`

`routers/batches.py` `_is_live` (97): `live[profile_id] = await agent_profiles_repo.is_live_profile(db, profile_id, channel="voice")`

`routers/admin_contacts.py` assign handler — before the `contacts_repo.assign_profile(...)` call (~62), reject a chat target (the picker already filters; this is the write-side guard; channel-only so an unpublished voice profile stays assignable as today):

```python
    if body.agent_profile_id is not None:
        target = await profiles_repo.get_profile(db, body.agent_profile_id)
        if target is not None and target.channel != "voice":
            raise HTTPException(status_code=422, detail="agent_profile_id must reference a voice agent")
```

`profiles_repo` is already imported in `admin_contacts.py` (`from usan_api.repositories import agent_profiles as profiles_repo`).

- [ ] **Step 5: Run the new test + voice regressions**

Run: `uv run pytest -n0 tests/test_agent_channel_callers.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_compat_agents.py tests/compat/test_freeze_agents.py tests/test_admin_profiles.py tests/test_admin_contacts.py tests/test_outbound_calls.py tests/test_batches.py -q`
Expected: PASS (voice agents are `channel='voice'`; the new args are no-ops for them).

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/compat/agent_bridge.py apps/api/src/usan_api/compat/routers/agents.py apps/api/src/usan_api/routers/admin_profiles.py apps/api/src/usan_api/routers/admin_contacts.py apps/api/src/usan_api/services/outbound_calls.py apps/api/src/usan_api/compat/call_create.py apps/api/src/usan_api/compat/batch_create.py apps/api/src/usan_api/routers/batches.py apps/api/tests/test_agent_channel_callers.py
git commit -m "feat(api): thread channel='voice' through agent_bridge + every voice caller (retell-llm stays agnostic)"
```

---

### Task 4: `compat/schemas/chat_agents.py`

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/chat_agents.py`
- Test: `apps/api/tests/compat/test_chat_agent_schemas.py`

**Interfaces:**
- Produces: `ChatAgentCreateRequest` (`response_engine: ResponseEngine` required), `ChatAgentUpdateRequest` (all optional), `ChatAgentResponse` (echo, `extra='allow'`). Reuses `ResponseEngine` from `schemas/agents.py`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_agent_schemas.py
"""Phase 4c-1: chat-agent request/response schema shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.chat_agents import (
    ChatAgentCreateRequest,
    ChatAgentResponse,
    ChatAgentUpdateRequest,
)


def test_create_requires_response_engine():
    with pytest.raises(ValidationError):
        ChatAgentCreateRequest(agent_name="x")
    ok = ChatAgentCreateRequest(response_engine={"type": "retell-llm", "llm_id": "llm_abc"})
    assert ok.response_engine.llm_id == "llm_abc"


def test_update_all_optional_and_echoes_extras():
    body = ChatAgentUpdateRequest(auto_close_message="bye")  # extra='allow'
    assert body.response_engine is None
    assert body.model_dump()["auto_close_message"] == "bye"


def test_response_echoes_extra_fields():
    resp = ChatAgentResponse(
        agent_id="agent_x",
        response_engine={"type": "retell-llm", "llm_id": "llm_x"},
        version=1,
        is_published=True,
        last_modification_timestamp=123,
        auto_close_message="bye",  # extra='allow'
    )
    dumped = resp.model_dump(exclude_none=True)
    assert dumped["auto_close_message"] == "bye"
    assert "agent_name" not in dumped  # None omitted
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -n0 tests/compat/test_chat_agent_schemas.py -q`
Expected: FAIL — `ModuleNotFoundError: usan_api.compat.schemas.chat_agents`.

- [ ] **Step 3: Create the schemas module**

```python
# apps/api/src/usan_api/compat/schemas/chat_agents.py
"""Pydantic models for the RetellAI-compatible chat-agent endpoints (Phase 4c-1).

A chat agent overlays an AgentProfile (channel='chat'); the CRM's submitted ChatAgentRequest
config is echoed verbatim via compat_extras['chat_agent']. Every model is ``extra='allow'`` so a
migrating CRM is never rejected for a chat-config field the engine persists-not-honors.
``response_engine`` is required on create, optional on update (PATCH semantics).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat.schemas.agents import ResponseEngine


class ChatAgentCreateRequest(BaseModel):
    """POST /create-chat-agent. ``response_engine.llm_id`` binds the chat agent onto the profile
    a prior ``create-retell-llm`` made (only ``type='retell-llm'`` is honored)."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine
    agent_name: str | None = None


class ChatAgentUpdateRequest(BaseModel):
    """PATCH /update-chat-agent — every field optional (partial update)."""

    model_config = ConfigDict(extra="allow")

    response_engine: ResponseEngine | None = None
    agent_name: str | None = None


class ChatAgentResponse(BaseModel):
    """The RetellAI chat-agent object. ``extra='allow'`` echoes the CRM's submitted config
    (held in compat_extras['chat_agent']) alongside the engine-derived fields. Net oracle-required
    fields: agent_id, response_engine, last_modification_timestamp."""

    model_config = ConfigDict(extra="allow")

    agent_id: str
    response_engine: dict[str, Any]
    version: int
    is_published: bool
    last_modification_timestamp: int | None = None
    agent_name: str | None = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -n0 tests/compat/test_chat_agent_schemas.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/chat_agents.py apps/api/tests/compat/test_chat_agent_schemas.py
git commit -m "feat(api): add compat chat-agent request/response schemas"
```

---

### Task 5: `compat/chat_agent_bridge.py` — serialize + CRUD

**Files:**
- Create: `apps/api/src/usan_api/compat/chat_agent_bridge.py`
- Test: `apps/api/tests/compat/test_chat_agent_bridge.py` (service-level)

**Interfaces:**
- Consumes: `agent_bridge` helpers (`_load_active`, `_config_dict`, `_merge_extras`, `_validate_config`, `_publish_and_commit`, `_unique_name`, `list_agent_profiles`, `_EXTRAS_KEY`, `_ACTOR`), Task 4 schemas, Task 3's `_load_active(expected_channel=…)` / `list_agent_profiles(channel=…)`.
- Produces: `serialize_chat_agent(profile)`, `serialize_chat_agent_version(profile, version_row)`, `create_chat_agent`, `get_chat_agent`, `list_chat_agents`, `list_chat_agent_versions`, `update_chat_agent`, `delete_chat_agent`, `publish_chat_agent`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_agent_bridge.py
"""Phase 4c-1: chat_agent_bridge create/serialize round-trip + channel stamping."""

from __future__ import annotations

import pytest

from usan_api.compat import agent_bridge, chat_agent_bridge, ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import ChatAgentCreateRequest
from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest
from usan_api.repositories import agent_profiles as repo  # noqa: F401  (mirror seed helpers if needed)
from usan_api.tenancy import set_tenant_context


@pytest.mark.asyncio
async def test_create_chat_agent_stamps_channel_and_serializes(app_session, seeded_org_id, settings):
    await set_tenant_context(app_session, seeded_org_id)
    llm = await agent_bridge.create_response_engine(
        app_session, settings, CreateRetellLlmRequest(general_prompt="hi")
    )
    await set_tenant_context(app_session, seeded_org_id)  # create_response_engine commits
    body = ChatAgentCreateRequest(
        response_engine={"type": "retell-llm", "llm_id": ids.encode_llm_id(llm.id)},
        agent_name="Chat Bot",
    )
    profile = await chat_agent_bridge.create_chat_agent(app_session, settings, body)
    assert profile.channel == "chat"
    payload = chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)
    assert payload["agent_id"] == ids.encode_agent_id(profile.id)
    assert payload["response_engine"]["type"] == "retell-llm"
    assert payload["is_published"] is True


@pytest.mark.asyncio
async def test_create_chat_agent_rejects_non_retell_llm(app_session, seeded_org_id, settings):
    await set_tenant_context(app_session, seeded_org_id)
    body = ChatAgentCreateRequest(response_engine={"type": "custom-llm", "llm_id": None})
    with pytest.raises(CompatError) as exc:
        await chat_agent_bridge.create_chat_agent(app_session, settings, body)
    assert exc.value.status_code == 422
```

Mirror `tests/compat/test_create_web_call_service.py` (or another service-level compat test) for the exact `settings` fixture + the published-profile seeding/commit pattern.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -n0 tests/compat/test_chat_agent_bridge.py -q`
Expected: FAIL — `ModuleNotFoundError: usan_api.compat.chat_agent_bridge`.

- [ ] **Step 3: Create the bridge module**

```python
# apps/api/src/usan_api/compat/chat_agent_bridge.py
"""Bridge the RetellAI chat-agent contract onto native AgentProfile (Phase 4c-1).

A chat agent is an AgentProfile with channel='chat' — the SAME overlay as a voice agent
(agent_bridge), minus the voice fields. ``create-chat-agent`` binds the agent half onto the
profile its ``response_engine.llm_id`` points at (a prior ``create-retell-llm``), stamps
channel='chat', and publishes. The submitted ChatAgentRequest config is echoed verbatim via
compat_extras['chat_agent'] (persisted-not-honored; the analysis config is consumed by 4c-2).
Reuses agent_bridge's overlay helpers so the voice/chat overlays stay in lockstep.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import agent_bridge, ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import (
    ChatAgentCreateRequest,
    ChatAgentResponse,
    ChatAgentUpdateRequest,
)
from usan_api.compat.serialization import to_ms
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.settings import Settings

_CHANNEL = "chat"
_EXTRAS_HALF = "chat_agent"


def _require_retell_llm(body: ChatAgentCreateRequest | ChatAgentUpdateRequest) -> uuid.UUID:
    engine = body.response_engine
    if engine is None or engine.type != "retell-llm" or not engine.llm_id:
        raise CompatError(422, "response_engine must be a retell-llm with an llm_id")
    return ids.decode_llm_id(engine.llm_id)


async def create_chat_agent(
    db: AsyncSession, settings: Settings, body: ChatAgentCreateRequest
) -> AgentProfile:
    """create-chat-agent: bind the chat config onto the response-engine's profile, mark it
    channel='chat', publish. Never sets the call-plane default flags."""
    profile = await agent_bridge._load_active(
        db, _require_retell_llm(body), kind="response engine"
    )
    config = agent_bridge._config_dict(profile)
    agent_bridge._merge_extras(config, _EXTRAS_HALF, body.model_dump())
    agent_bridge._validate_config(config)
    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=agent_bridge._ACTOR
    )
    if updated is None:  # pragma: no cover - loaded active above
        raise CompatError(404, "response engine not found")
    updated.channel = _CHANNEL
    if body.agent_name:
        updated.name = await agent_bridge._unique_name(db, body.agent_name, exclude_id=updated.id)
    await agent_bridge._publish_and_commit(db, profile.id, note="compat create-chat-agent")
    await db.refresh(updated)
    return updated


async def get_chat_agent(db: AsyncSession, agent_id: str) -> AgentProfile:
    return await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )


async def list_chat_agents(db: AsyncSession) -> list[AgentProfile]:
    return await agent_bridge.list_agent_profiles(db, channel=_CHANNEL)


async def list_chat_agent_versions(
    db: AsyncSession, agent_id: str
) -> tuple[AgentProfile, list[AgentProfileVersion]]:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    versions = await agent_profiles_repo.list_versions(db, profile.id)
    return profile, versions


async def update_chat_agent(
    db: AsyncSession, settings: Settings, agent_id: str, body: ChatAgentUpdateRequest
) -> AgentProfile:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    config = agent_bridge._config_dict(profile)
    agent_bridge._merge_extras(config, _EXTRAS_HALF, body.model_dump(exclude_none=True))
    agent_bridge._validate_config(config)
    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=agent_bridge._ACTOR
    )
    if updated is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    if body.agent_name:
        updated.name = await agent_bridge._unique_name(db, body.agent_name, exclude_id=updated.id)
    await agent_bridge._publish_and_commit(db, profile.id, note="compat update-chat-agent")
    await db.refresh(updated)
    return updated


async def delete_chat_agent(db: AsyncSession, agent_id: str) -> None:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    try:
        archived = await agent_profiles_repo.archive_profile(db, profile.id)
    except ProfileInUseError as exc:
        raise CompatError(409, str(exc)) from exc
    if archived is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    await db.commit()


async def publish_chat_agent(db: AsyncSession, agent_id: str) -> None:
    """publish-chat-agent (deprecated, 200 no body): publish the latest draft."""
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    version = await agent_profiles_repo.publish(
        db, profile.id, note="compat publish-chat-agent", actor_email=agent_bridge._ACTOR
    )
    if version is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    await db.commit()


def serialize_chat_agent(profile: AgentProfile) -> ChatAgentResponse:
    config = profile.draft_config or {}
    extras = (config.get(agent_bridge._EXTRAS_KEY) or {}).get(_EXTRAS_HALF) or {}
    data: dict[str, Any] = dict(extras)  # echo the CRM's submitted chat config
    published = profile.published_version
    data.update(
        {
            "agent_id": ids.encode_agent_id(profile.id),
            "agent_name": profile.name,
            "response_engine": {
                "type": "retell-llm",
                "llm_id": ids.encode_llm_id(profile.id),
                "version": published or 0,
            },
            "version": published or 0,
            "is_published": published is not None,
            "last_modification_timestamp": to_ms(profile.updated_at),
        }
    )
    return ChatAgentResponse(**data)


def serialize_chat_agent_version(
    profile: AgentProfile, version_row: AgentProfileVersion
) -> ChatAgentResponse:
    base = serialize_chat_agent(profile)
    return base.model_copy(
        update={
            "version": version_row.version,
            "last_modification_timestamp": to_ms(version_row.published_at),
            "is_published": True,
        }
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -n0 tests/compat/test_chat_agent_bridge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/chat_agent_bridge.py apps/api/tests/compat/test_chat_agent_bridge.py
git commit -m "feat(api): add chat_agent_bridge (serialize + CRUD over the agent_profiles overlay)"
```

---

### Task 6: `compat/routers/chat_agents.py` + register + un-stub

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/chat_agents.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (import + `include_router`)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the 7 chat-agent entries 45-52)
- Test: `apps/api/tests/compat/test_chat_agents_router.py`

**Interfaces:**
- Consumes: Task 5 bridge, Task 4 schemas.
- Produces: 7 served routes at the exact oracle paths; `KNOWN_GAPS` stays empty.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/compat/test_chat_agents_router.py
"""Phase 4c-1: chat-agent router happy paths + cross-resource isolation."""

from __future__ import annotations


def _create_chat_agent(compat_client, compat_headers) -> dict:
    llm = compat_client.post(
        "/create-retell-llm",
        json={"general_prompt": "You are a helpful chat assistant."},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-chat-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "agent_name": "Chat Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers):
    created = _create_chat_agent(compat_client, compat_headers)
    agent_id = created["agent_id"]
    got = compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers)
    assert got.status_code == 200, got.text
    assert got.json()["agent_id"] == agent_id


def test_list_chat_agents_returns_only_chat(compat_client, compat_headers):
    created = _create_chat_agent(compat_client, compat_headers)
    items = compat_client.get("/list-chat-agents", headers=compat_headers).json()
    assert isinstance(items, list)
    assert any(i["agent_id"] == created["agent_id"] for i in items)


def test_delete_then_404(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    assert compat_client.delete(f"/delete-chat-agent/{agent_id}", headers=compat_headers).status_code == 204
    assert compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers).status_code == 404


def test_get_agent_404s_on_chat_id(compat_client, compat_headers):
    """Cross-resource isolation: the VOICE get-agent op must 404 on a chat-agent id."""
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    assert compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).status_code == 404


def test_publish_chat_agent_200(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    r = compat_client.post(f"/publish-chat-agent/{agent_id}", headers=compat_headers)
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -n0 tests/compat/test_chat_agents_router.py -q`
Expected: FAIL — `/create-chat-agent` returns 501 (still stubbed).

- [ ] **Step 3: Create the router**

```python
# apps/api/src/usan_api/compat/routers/chat_agents.py
"""RetellAI-compatible chat-agent endpoints (Phase 4c-1):

  POST   /create-chat-agent                       (201)
  GET    /get-chat-agent/{agent_id}
  GET    /get-chat-agent-versions/{agent_id}
  GET    /list-chat-agents                        (bare array, deprecated)
  PATCH  /update-chat-agent/{agent_id}
  DELETE /delete-chat-agent/{agent_id}            (204)
  POST   /publish-chat-agent/{agent_id}           (200, no body, deprecated)

A chat agent overlays an AgentProfile (channel='chat'). ``response_model`` is omitted so the
``extra='allow'`` ChatAgentResponse echo survives serialization (model_dump(exclude_none=True)).
Auth + org-scoped RLS via get_compat_db; every op emits a PHI-free audit line.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import chat_agent_bridge, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import ChatAgentCreateRequest, ChatAgentUpdateRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-chat-agents"])


def _audit(request: Request, op: str, agent_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, agent_id=agent_id).info("compat chat-agent op={op}")


@router.post("/create-chat-agent", status_code=status.HTTP_201_CREATED)
async def create_chat_agent(
    body: ChatAgentCreateRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.create_chat_agent(db, settings, body)
    response.status_code = status.HTTP_201_CREATED
    _audit(request, "create-chat-agent", ids.encode_agent_id(profile.id))
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.get("/get-chat-agent/{agent_id}")
async def get_chat_agent(
    agent_id: str,
    request: Request,
    # ?version accepted (AgentVersionReference); the current published view is always served.
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.get_chat_agent(db, agent_id)
    _audit(request, "get-chat-agent", agent_id)
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.get("/get-chat-agent-versions/{agent_id}")
async def get_chat_agent_versions(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    profile, versions = await chat_agent_bridge.list_chat_agent_versions(db, agent_id)
    _audit(request, "get-chat-agent-versions", agent_id)
    return [
        chat_agent_bridge.serialize_chat_agent_version(profile, v).model_dump(exclude_none=True)
        for v in versions
    ]


@router.get("/list-chat-agents")
async def list_chat_agents(
    request: Request,
    pagination_key: str | None = Query(default=None),
    pagination_key_version: int | None = Query(default=None),
    is_latest: bool | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_compat_db),
) -> list[dict[str, Any]]:
    """Bare array (deprecated). Keyset cursor over (name, id); ``is_latest`` /
    ``pagination_key_version`` are accepted for contract compatibility and the current published
    view is returned per profile."""
    profiles = await chat_agent_bridge.list_chat_agents(db)
    profiles = sorted(profiles, key=lambda p: (p.name, p.id.bytes))
    if pagination_key:
        with contextlib.suppress(CompatError):
            after = ids.decode_agent_id(pagination_key)
            cut = next((i for i, p in enumerate(profiles) if p.id == after), None)
            if cut is not None:
                profiles = profiles[cut + 1 :]
    _audit(request, "list-chat-agents")
    return [
        chat_agent_bridge.serialize_chat_agent(p).model_dump(exclude_none=True)
        for p in profiles[:limit]
    ]


@router.patch("/update-chat-agent/{agent_id}")
async def update_chat_agent(
    agent_id: str,
    body: ChatAgentUpdateRequest,
    request: Request,
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    profile = await chat_agent_bridge.update_chat_agent(db, settings, agent_id, body)
    _audit(request, "update-chat-agent", agent_id)
    return chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)


@router.delete("/delete-chat-agent/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_agent_bridge.delete_chat_agent(db, agent_id)
    _audit(request, "delete-chat-agent", agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/publish-chat-agent/{agent_id}")
async def publish_chat_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    """Deprecated thin publish — the oracle returns 200 with no response body."""
    await chat_agent_bridge.publish_chat_agent(db, agent_id)
    _audit(request, "publish-chat-agent", agent_id)
    return Response(status_code=status.HTTP_200_OK)
```

- [ ] **Step 4: Register the router + remove the 501 stubs**

In `compat/app.py`, add the import (after `compat_chats`):

```python
from usan_api.compat.routers import chat_agents as compat_chat_agents
```

and the include (after `app.include_router(compat_chats.router)`):

```python
    app.include_router(compat_chat_agents.router)
```

In `compat/routers/unsupported.py`, DELETE the 7 chat-agent tuple entries (the `# --- Chat agent ---` block, lines 45-52):

```python
    # --- Chat agent ---
    ("POST", "/create-chat-agent"),
    ("GET", "/list-chat-agents"),
    ("GET", "/get-chat-agent/{agent_id}"),
    ("GET", "/get-chat-agent-versions/{agent_id}"),
    ("PATCH", "/update-chat-agent/{agent_id}"),
    ("DELETE", "/delete-chat-agent/{agent_id}"),
    ("POST", "/publish-chat-agent/{agent_id}"),
```

Leave `("PUT", "/rerun-chat-analysis/{chat_id}")` (line 81) as a stub — it is Phase 4c-2.

- [ ] **Step 5: Run the router test + surface coverage**

Run: `uv run pytest -n0 tests/compat/test_chat_agents_router.py tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -q`
Expected: PASS. Surface coverage stays green (routes now in `app.routes`, stubs removed); `KNOWN_GAPS` unchanged.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/compat/routers/chat_agents.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/test_chat_agents_router.py
git commit -m "feat(api): serve the 7 chat-agent ops (router + register + remove 501 stubs)"
```

---

### Task 7: Frozen conformance + leak regression + docs

**Files:**
- Create: `apps/api/tests/compat/test_freeze_chat_agents.py`
- Create: `apps/api/tests/compat/test_chat_agent_isolation.py`
- Modify: `apps/api/tests/compat/conformance.py` (header doc map ~3-12)
- Create: `docs/deployment/chat-agents.md`

**Interfaces:**
- Consumes: Tasks 1-6.

- [ ] **Step 1: Write the frozen conformance test**

```python
# apps/api/tests/compat/test_freeze_chat_agents.py
"""Contract-freeze tests for the RetellAI-compatible chat-agent surface (Phase 4c-1).

Pins that create/get/list/version chat-agent responses conform to the oracle ChatAgentResponse
component AND round-trip through retell-sdk 5.53.0's ChatAgentResponse model.
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def _create_chat_agent(compat_client, compat_headers) -> dict:
    llm = compat_client.post(
        "/create-retell-llm",
        json={"general_prompt": "You are a helpful chat assistant."},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-chat-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "agent_name": "Freeze Chat Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_chat_agent_conforms(compat_client, compat_headers):
    payload = _create_chat_agent(compat_client, compat_headers)
    assert_conforms(payload, "ChatAgentResponse")
    assert_sdk_roundtrip(payload, "retell.types:ChatAgentResponse")
    assert "base_version" not in payload  # optional, omitted via exclude_none
    assert "assigned_tags" not in payload


def test_get_chat_agent_conforms(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    r = compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers)
    assert r.status_code == 200, r.text
    assert_conforms(r.json(), "ChatAgentResponse")
    assert_sdk_roundtrip(r.json(), "retell.types:ChatAgentResponse")


def test_list_chat_agents_items_conform(compat_client, compat_headers):
    _create_chat_agent(compat_client, compat_headers)
    items = compat_client.get("/list-chat-agents", headers=compat_headers).json()
    assert items
    for item in items:
        assert_conforms(item, "ChatAgentResponse")
        assert_sdk_roundtrip(item, "retell.types:ChatAgentResponse")


def test_get_chat_agent_versions_conform(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    compat_client.patch(
        f"/update-chat-agent/{agent_id}", json={"agent_name": "v2"}, headers=compat_headers
    )
    versions = compat_client.get(
        f"/get-chat-agent-versions/{agent_id}", headers=compat_headers
    ).json()
    assert isinstance(versions, list) and versions
    for v in versions:
        assert v["agent_id"] == agent_id
        assert_conforms(v, "ChatAgentResponse")
        assert_sdk_roundtrip(v, "retell.types:ChatAgentResponse")
```

- [ ] **Step 2: Write the leak / cross-resource isolation test**

```python
# apps/api/tests/compat/test_chat_agent_isolation.py
"""Phase 4c-1: a chat agent never leaks into the voice surfaces; retell-llm stays agnostic."""

from __future__ import annotations

from tests.compat.conftest import RETELL_VOICE


def _make_chat(compat_client, compat_headers) -> dict:
    llm = compat_client.post(
        "/create-retell-llm", json={"general_prompt": "chat"}, headers=compat_headers
    ).json()
    r = compat_client.post(
        "/create-chat-agent",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]}},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return {"agent_id": r.json()["agent_id"], "llm_id": llm["llm_id"]}


def _make_voice(compat_client, compat_headers) -> str:
    llm = compat_client.post(
        "/create-retell-llm", json={"general_prompt": "voice"}, headers=compat_headers
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["agent_id"]


def test_voice_list_agents_excludes_chat(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    voice_id = _make_voice(compat_client, compat_headers)
    items = compat_client.get("/list-agents", headers=compat_headers).json()
    ids = {i["agent_id"] for i in items}
    assert voice_id in ids
    assert chat["agent_id"] not in ids


def test_v2_list_agents_excludes_chat(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    body = compat_client.post("/v2/list-agents", json={}, headers=compat_headers).json()
    ids = {i["agent_id"] for i in body["items"]}
    assert chat["agent_id"] not in ids


def test_list_retell_llms_includes_chat_bound_llm(compat_client, compat_headers):
    """A Retell-LLM is channel-agnostic infra: list-retell-llms must still show a chat-bound LLM."""
    chat = _make_chat(compat_client, compat_headers)
    llms = compat_client.get("/list-retell-llms", headers=compat_headers).json()
    assert any(item["llm_id"] == chat["llm_id"] for item in llms)


def test_get_retell_llm_works_on_chat_bound(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    r = compat_client.get(f"/get-retell-llm/{chat['llm_id']}", headers=compat_headers)
    assert r.status_code == 200, r.text


def test_get_chat_agent_404s_on_voice_id(compat_client, compat_headers):
    voice_id = _make_voice(compat_client, compat_headers)
    assert compat_client.get(f"/get-chat-agent/{voice_id}", headers=compat_headers).status_code == 404
```

- [ ] **Step 3: Run both new test files**

Run: `uv run pytest -n0 tests/compat/test_freeze_chat_agents.py tests/compat/test_chat_agent_isolation.py -q`
Expected: PASS.

If `assert_conforms(..., "ChatAgentResponse")` fails on the `response_engine` oneOf, confirm the emitted `response_engine` is exactly `{"type": "retell-llm", "llm_id": "...", "version": N}` (the retell-llm member); if a oneOf-ambiguity arises, drop the `version` key (it is optional) — do NOT add fields. If `assert_sdk_roundtrip` fails, re-confirm the SDK class name `retell.types:ChatAgentResponse` resolves (it is re-exported from `retell/types/__init__.py`).

- [ ] **Step 4: Add the conformance header map line**

In `tests/compat/conformance.py`, in the module docstring's "Discovered oracle component / SDK model names" list (~line 11), add:

```
  ChatAgent:   oracle='ChatAgentResponse'      sdk='retell.types:ChatAgentResponse'
```

- [ ] **Step 5: Write the operator doc**

```markdown
# chat-agent CRUD (Phase 4c-1) — operator note

RetellAI-compatible chat-agent management: `POST /create-chat-agent`,
`GET /get-chat-agent/{agent_id}`, `GET /get-chat-agent-versions/{agent_id}`,
`GET /list-chat-agents`, `PATCH /update-chat-agent/{agent_id}`,
`DELETE /delete-chat-agent/{agent_id}`, `POST /publish-chat-agent/{agent_id}`.

A chat agent is an `agent_profiles` row with `channel='chat'` (the same overlay as a voice
agent). It is created by the two-step flow `create-retell-llm` (holds the prompt) →
`create-chat-agent` (binds `response_engine.llm_id` + chat config, marks channel='chat').

## Enable
1. Apply migration `0045` (adds `agent_profiles.channel TEXT NOT NULL DEFAULT 'voice'`).
   Owner-DDL — the deploy migrates as the `usan` owner; the new column inherits the table's
   `usan_app` GRANT + RLS policy.
2. Mint a compat key (super-admin UI) — all compat ops 401 until a key exists.

No new env keys. Inert until a `v*` tag deploys migration 0045.

## Behavior / posture
- Only `response_engine.type='retell-llm'` is honored (`custom-llm`/`conversation-flow` → 422).
- Chat config (`auto_close_message`, `end_chat_after_silence_ms`, `post_chat_analysis_data`,
  `post_chat_analysis_model`, `pii_config`, `guardrail_config`, `handbook_config`,
  `data_storage_*`, `webhook_*`, `language`, `timezone`, `version_title`) is echoed verbatim and
  **persisted-not-honored** (the analysis config is consumed by Phase 4c-2's rerun-chat-analysis).
- `version` query (`AgentVersionReference`) is accepted; the current published view is returned.
  `base_version`/`assigned_tags` are omitted; `is_latest`/`pagination_key_version` accepted-and-ignored.
- Writes always publish; delete = archive; publish = thin 200-no-body (deprecated).
- **Isolation:** a chat agent never appears in the voice `list-agents`/`v2/list-agents`,
  `GET /v1/admin/profiles` (or its pickers), and can never be dialed, set as a call default,
  used as a `profile_override`, or assigned to a contact. The voice `get-agent`/`update-agent`/
  etc. 404 on a chat id and `get-chat-agent` 404s on a voice id. Retell-LLM ops are
  channel-agnostic (an LLM is shared infra).

## Deferred
- `rerun-chat-analysis` + the `chat_analysis` pipeline → Phase 4c-2.
- `create-chat`/`create-sms-chat` (4a/4b) are NOT tightened to require `channel='chat'` — a chat
  session may still open against a voice agent_id (same-org, RLS-scoped; not a safety leak).
```

- [ ] **Step 6: Full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all green; single alembic head 0045.

- [ ] **Step 7: Commit**

```bash
git add apps/api/tests/compat/test_freeze_chat_agents.py apps/api/tests/compat/test_chat_agent_isolation.py apps/api/tests/compat/conformance.py docs/deployment/chat-agents.md
git commit -m "test(api): freeze + isolation tests for chat-agent CRUD; operator doc"
```

---

## Self-Review (plan author)

**Spec coverage:** §2 ops → Tasks 5/6; §3 backing + migration → Task 1; §4 leak audit → Tasks 2/3 (+ isolation tests in 7); §5 serialization → Task 5; §6 op semantics → Tasks 5/6; §7 files → all tasks; §8 testing → Tasks 1-7; §9 posture/deviations → doc (Task 7) + behavior; §10 out-of-scope rerun stays stubbed (Task 6 leaves line 81); §11 global constraints → header. Covered.

**Deviations from spec (intentional, flagged):**
- Chat-agent bridge lives in `compat/chat_agent_bridge.py` reusing `agent_bridge` helpers via module access (spec said "sibling reusing helpers" — same intent, avoids private cross-imports and leaves voice code untouched).
- `assign_profile` guard implemented in the single caller (`admin_contacts` handler, channel-only) rather than the repo — avoids tightening liveness and threading a new exception (spec §4.3 intent preserved).
- `create_profile` is NOT explicitly stamped `channel='voice'` (the `server_default` backfills it); only the re-bindable paths (`bind_agent`→voice, `create_chat_agent`→chat) stamp explicitly (spec §4.2 intent preserved; DRY).

**Placeholder scan:** none — every code step shows full code.

**Type consistency:** `channel`/`expected_channel` params default `None` everywhere; `list_agent_profiles(channel=…)`, `_load_active(expected_channel=…)`, `is_live_profile(channel=…)`, `list_profiles(channel=…)` consistent across Tasks 2/3/5; `serialize_chat_agent` returns `ChatAgentResponse` (Task 4) consumed by the Task 6 router. Consistent.
