# RetellAI Parity Phase 4b-2 — Inbound Two-Way SMS Reply Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the recipient of an `sms_chat` texts back, match the open session, generate an agent reply via the existing Vertex path, persist both turns, and send the reply via Telnyx — making `sms_chat` sessions conversational with no new served compat operation.

**Architecture:** Extend the existing `POST /webhooks/telnyx` handler with a reply-engine branch (ordered opt-out → sms-reply → family-task). A new `compat/sms_reply.py` matches the open `sms_chat` (FOR UPDATE), persists the inbound turn as `role="sms"` (oracle-faithful) with a `provider_message_id` for dedup, reuses an extracted `generate_agent_reply` helper for the Vertex turn, and sends the reply. Inert behind a new `telnyx_inbound_sms_reply_enabled` flag.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, pydantic-settings, Telnyx Ed25519 webhooks, Vertex AI (`run_vertex_turn`), pytest + testcontainers Postgres.

**Spec:** `docs/superpowers/specs/2026-06-27-retell-parity-phase4b2-inbound-sms-reply-design.md`

## Global Constraints

- Migration `0044`, `down_revision="0043"`; additive, RLS-scoped (TenantScoped + FORCE RLS convention), owner-applied on deploy. `revision`/`down_revision` use the **typed** form (`revision: str = "0044"`), matching `0043`.
- Inbound recipient turn stored as exactly `role="sms"`; agent reply as `role="agent"`.
- `handle_inbound_sms_reply` returns `True` for every matched-session case (reply / dedup / unconfigured / failed), `False` only for flag-off / empty `to_number` / no match.
- `db.commit()` sits **outside** the send `try` (a commit wrapped in the send-`except` would mislabel a commit failure as a send failure → double-send risk).
- The webhook always returns 200 for a signature-valid, parseable `message.received`; only forged/stale signature → 401 and bad JSON → 400 (both pre-existing, unchanged).
- On any reply-engine failure: `await db.rollback()` (whole txn, no orphan PHI) + a `WEBHOOKS_TOTAL` metric + a log carrying only `message_id` + `type(exc).__name__` (never `from`/`to`/text/reply body/vars); return `True`.
- `organization_id` is server-set by RLS; app code never sets it. The matcher omits any `organization_id` predicate (RLS scopes to the default org).
- `apps/api` and `services/agent` do not import each other (the engine stays within `apps/api`/`compat`).
- No `v*` tag. Inert until a deploy applies `0044` and the operator sets `telnyx_inbound_sms_reply_enabled` + `telnyx_messaging_*` + `gcp_project` + `telnyx_inbound_public_key`.
- Run `cd apps/api && uv run pytest` (parallel) and `uv run mypy` + `ruff check . && ruff format .` before each commit.

---

### Task 1: Migration 0044 + ChatMessage.provider_message_id + indexes

**Files:**
- Create: `apps/api/migrations/versions/0044_chat_provider_message_id.py`
- Modify: `apps/api/src/usan_api/db/models.py` (`ChatMessage` ~line 1311, `ChatSession` ~line 1273)
- Test: `apps/api/tests/compat/test_provider_message_id.py` (create)

**Interfaces:**
- Produces: `chat_messages.provider_message_id TEXT NULL`; partial-unique index `uq_chat_messages_provider_msg (organization_id, provider_message_id) WHERE provider_message_id IS NOT NULL`; index `ix_chat_sessions_sms_match (from_number, to_number) WHERE chat_type = 'sms_chat'`. `ChatMessage.provider_message_id: Mapped[str | None]`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_provider_message_id.py`:

```python
"""chat_messages gains provider_message_id + a partial-unique dedup index (Phase 4b-2)."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, ChatMessage, ChatSession
from usan_api.tenant_context import set_tenant_context


async def _seed_session(db) -> ChatSession:
    profile = AgentProfile(
        name=f"SMS Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    s = ChatSession(agent_profile_id=profile.id, agent_version=1, chat_type="sms_chat", dynamic_vars={})
    db.add(s)
    await db.flush()
    return s


def test_migration_0044_revision_header() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations" / "versions" / "0044_chat_provider_message_id.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0044", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0044"
    assert mod.down_revision == "0043"


@pytest.mark.asyncio
async def test_provider_message_id_persists_and_dedups(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    s = await _seed_session(app_session)

    app_session.add(ChatMessage(chat_session_id=s.id, seq=1, role="sms", content="hi", provider_message_id="tx-1"))
    await app_session.flush()
    # a duplicate provider id in the same org violates the partial unique
    app_session.add(ChatMessage(chat_session_id=s.id, seq=2, role="sms", content="dup", provider_message_id="tx-1"))
    with pytest.raises(IntegrityError):
        await app_session.flush()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_null_provider_message_id_is_not_deduped(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    s = await _seed_session(app_session)
    # two NULL provider ids coexist (api_chat rows) — the partial index excludes NULLs
    app_session.add(ChatMessage(chat_session_id=s.id, seq=1, role="user", content="a"))
    app_session.add(ChatMessage(chat_session_id=s.id, seq=2, role="agent", content="b"))
    await app_session.flush()
    await app_session.rollback()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_provider_message_id.py -q`
Expected: FAIL — `0044_*` file missing; `ChatMessage` has no `provider_message_id`.

- [ ] **Step 3: Add the model column + indexes**

In `apps/api/src/usan_api/db/models.py`, add to `ChatMessage` after the `role`/`content` columns:

```python
    provider_message_id: Mapped[str | None] = mapped_column(Text)
```

Replace `ChatMessage.__table_args__` (currently just `uq_chat_messages_session_seq`) with:

```python
    __table_args__ = (
        UniqueConstraint("chat_session_id", "seq", name="uq_chat_messages_session_seq"),
        Index(
            "uq_chat_messages_provider_msg",
            "organization_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
    )
```

Replace `ChatSession.__table_args__` (currently the started_at index) with:

```python
    __table_args__ = (
        Index("ix_chat_sessions_started_at_id", "started_at", "id"),
        Index(
            "ix_chat_sessions_sms_match",
            "from_number",
            "to_number",
            postgresql_where=text("chat_type = 'sms_chat'"),
        ),
    )
```

(`Index` and `text` are already imported in `models.py`.)

- [ ] **Step 4: Create migration 0044**

Create `apps/api/migrations/versions/0044_chat_provider_message_id.py`:

```python
"""chat_messages: add provider_message_id + partial-unique dedup; sms-match index (Phase 4b-2).

Additive. provider_message_id is the Telnyx inbound message id used to dedup redeliveries
of an inbound SMS reply; the partial unique (organization_id, provider_message_id) only
constrains non-NULL rows so api_chat messages (NULL) are unaffected. ix_chat_sessions_sms_match
speeds the per-inbound open-sms_chat lookup. Owner-DDL migration — the deploy migrates as
the usan owner; the new column inherits the table's usan_app GRANT + RLS policy.

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("provider_message_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_chat_messages_provider_msg",
        "chat_messages",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_chat_sessions_sms_match",
        "chat_sessions",
        ["from_number", "to_number"],
        postgresql_where=sa.text("chat_type = 'sms_chat'"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_sms_match", table_name="chat_sessions")
    op.drop_index("uq_chat_messages_provider_msg", table_name="chat_messages")
    op.drop_column("chat_messages", "provider_message_id")
```

- [ ] **Step 5: Verify single head + tests pass**

Run: `cd apps/api && uv run alembic heads`
Expected: exactly one head — `0044 (head)`.

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_provider_message_id.py -q`
Expected: PASS (the testcontainer runs `alembic upgrade head` to 0044).

- [ ] **Step 6: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/migrations/versions/0044_chat_provider_message_id.py apps/api/src/usan_api/db/models.py apps/api/tests/compat/test_provider_message_id.py
git commit -m "feat(api): 0044 chat_messages.provider_message_id + sms dedup/match indexes (4b-2)"
```

---

### Task 2: Repo — `add_message(provider_message_id)` + `find_open_sms_chat`

**Files:**
- Modify: `apps/api/src/usan_api/repositories/chats.py`
- Test: `apps/api/tests/compat/test_find_open_sms_chat.py` (create)

**Interfaces:**
- Consumes: `ChatMessage.provider_message_id` (Task 1).
- Produces: `add_message(db, *, session_id, seq, role, content, provider_message_id: str | None = None) -> ChatMessage`; `find_open_sms_chat(db, *, our_number: str, recipient: str) -> ChatSession | None`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_find_open_sms_chat.py`:

```python
"""find_open_sms_chat + add_message(provider_message_id) (Phase 4b-2)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.repositories import chats as chats_repo
from usan_api.tenant_context import set_tenant_context

_OUR = "+15550000000"
_RECIP = "+15551234567"


async def _profile(db) -> AgentProfile:
    p = AgentProfile(
        name=f"A {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "x"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(p)
    await db.flush()
    return p


async def _sms_session(db, profile, *, frm, to, status=ChatStatus.ONGOING, chat_type="sms_chat"):
    s = ChatSession(
        agent_profile_id=profile.id, agent_version=1, chat_type=chat_type,
        dynamic_vars={}, from_number=frm, to_number=to, status=status,
    )
    db.add(s)
    await db.flush()
    return s


@pytest.mark.asyncio
async def test_matches_open_sms_chat(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    want = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    # decoys: ended, archived, wrong number, api_chat
    await _sms_session(app_session, p, frm=_OUR, to=_RECIP, status=ChatStatus.ENDED)
    await _sms_session(app_session, p, frm=_OUR, to="+19998887777")
    await _sms_session(app_session, p, frm=_OUR, to=_RECIP, chat_type="api_chat")

    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is not None and got.id == want.id
    await app_session.rollback()


@pytest.mark.asyncio
async def test_no_match_returns_none(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_multiple_open_picks_newest(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    old = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    # force a later started_at on the second row
    new = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    await app_session.execute(
        text("UPDATE chat_sessions SET started_at = now() + interval '1 hour' WHERE id = :i"),
        {"i": new.id},
    )
    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is not None and got.id == new.id and got.id != old.id
    await app_session.rollback()


@pytest.mark.asyncio
async def test_add_message_persists_provider_id(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    s = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    m = await chats_repo.add_message(
        app_session, session_id=s.id, seq=1, role="sms", content="hi", provider_message_id="tx-9"
    )
    await app_session.flush()
    assert m.provider_message_id == "tx-9"
    await app_session.rollback()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_find_open_sms_chat.py -q`
Expected: FAIL — `find_open_sms_chat` missing; `add_message` rejects `provider_message_id`.

- [ ] **Step 3: Implement the repo changes**

In `apps/api/src/usan_api/repositories/chats.py`, replace `add_message`:

```python
async def add_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    seq: int,
    role: str,
    content: str,
    provider_message_id: str | None = None,
) -> ChatMessage:
    message = ChatMessage(
        chat_session_id=session_id,
        seq=seq,
        role=role,
        content=content,
        provider_message_id=provider_message_id,
    )
    db.add(message)
    return message
```

Add `find_open_sms_chat` after `lock_session`:

```python
async def find_open_sms_chat(
    db: AsyncSession, *, our_number: str, recipient: str
) -> ChatSession | None:
    """The open sms_chat for an inbound reply: our number is the row's from_number and the
    sender (recipient of the original outbound) is its to_number. RLS scopes to the default
    org. FOR UPDATE serializes concurrent inbound turns; newest-started wins on ties."""
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.chat_type == "sms_chat",
            ChatSession.from_number == our_number,
            ChatSession.to_number == recipient,
            ChatSession.status == ChatStatus.ONGOING,
            ChatSession.archived_at.is_(None),
        )
        .order_by(ChatSession.started_at.desc(), ChatSession.id.desc())
        .limit(1)
        .with_for_update()
    )
    return (await db.execute(stmt)).scalars().first()
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_find_open_sms_chat.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/src/usan_api/repositories/chats.py apps/api/tests/compat/test_find_open_sms_chat.py
git commit -m "feat(api): chats repo find_open_sms_chat + add_message provider_message_id (4b-2)"
```

---

### Task 3: Settings flag + `InboundSms.to_number` parsing

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (Telnyx Messaging block, ~line 156)
- Modify: `apps/api/src/usan_api/schemas/inbound_sms.py`
- Test: `apps/api/tests/test_settings_messaging.py` (extend), `apps/api/tests/test_inbound_sms_to_number.py` (create)

**Interfaces:**
- Produces: `Settings.telnyx_inbound_sms_reply_enabled: bool` (default `False`, env `TELNYX_INBOUND_SMS_REPLY_ENABLED`); `InboundSms.to_number: str` (default `""`); `parse_inbound_sms` captures `payload.to[0].phone_number`.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_inbound_sms_to_number.py`:

```python
"""parse_inbound_sms captures the recipient (payload.to) for 4b-2 session matching."""

from __future__ import annotations

from usan_api.schemas.inbound_sms import parse_inbound_sms


def _payload(*, with_to: bool):
    inner = {"id": "m1", "from": {"phone_number": "+15551234567"}, "text": "hello"}
    if with_to:
        inner["to"] = [{"phone_number": "+15550000000"}]
    return {"data": {"event_type": "message.received", "payload": inner}}


def test_captures_to_number():
    parsed = parse_inbound_sms(_payload(with_to=True))
    assert parsed is not None
    assert parsed.to_number == "+15550000000"
    assert parsed.from_number == "+15551234567"


def test_to_number_absent_defaults_empty():
    parsed = parse_inbound_sms(_payload(with_to=False))
    assert parsed is not None
    assert parsed.to_number == ""
```

Add to `apps/api/tests/test_settings_messaging.py` (mirror its existing `telnyx_messaging_enabled` style):

```python
def test_inbound_sms_reply_disabled_by_default(monkeypatch):
    from usan_api.settings import get_settings

    monkeypatch.delenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().telnyx_inbound_sms_reply_enabled is False
    get_settings.cache_clear()


def test_inbound_sms_reply_enabled_via_env(monkeypatch):
    from usan_api.settings import get_settings

    monkeypatch.setenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().telnyx_inbound_sms_reply_enabled is True
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_inbound_sms_to_number.py tests/test_settings_messaging.py -q`
Expected: FAIL — no `to_number`; no `telnyx_inbound_sms_reply_enabled`.

- [ ] **Step 3: Add the settings flag**

In `apps/api/src/usan_api/settings.py`, add immediately after the `telnyx_messaging_enabled` line (in the Telnyx Messaging block):

```python
    # Phase 4b-2: gate the inbound two-way SMS auto-reply engine independently of the
    # outbound send flag, so it can be staged/rolled-back on its own. Default FALSE.
    telnyx_inbound_sms_reply_enabled: bool = Field(
        default=False, alias="TELNYX_INBOUND_SMS_REPLY_ENABLED"
    )
```

- [ ] **Step 4: Extend the inbound schema**

In `apps/api/src/usan_api/schemas/inbound_sms.py`, add `to_number` to `InboundSms`:

```python
class InboundSms(BaseModel):
    """The fields the family-task / opt-out intake needs from an inbound SMS."""

    message_id: str  # Telnyx message id — the idempotency key
    from_number: str  # E.164 sender; matched against family_contacts.phone_e164
    to_number: str = ""  # E.164 recipient (our number); 4b-2 session matching. "" when absent.
    text: str
    event_type: str
```

In `parse_inbound_sms`, after the `from_number` line, capture `to`:

```python
    sender = inner.get("from")
    from_number = sender.get("phone_number", "") if isinstance(sender, dict) else ""
    recipient = inner.get("to")
    to_number = (
        recipient[0].get("phone_number", "")
        if isinstance(recipient, list) and recipient and isinstance(recipient[0], dict)
        else ""
    )
    text = inner.get("text") or ""
    if event_type != "message.received" or not message_id or not from_number:
        return None
    return InboundSms(
        message_id=str(message_id),
        from_number=str(from_number),
        to_number=str(to_number),
        text=str(text)[:_MAX_SMS_TEXT_CHARS],
        event_type=event_type,
    )
```

- [ ] **Step 5: Run to verify they pass + existing parse test still green**

Run: `cd apps/api && uv run pytest -n0 tests/test_inbound_sms_to_number.py tests/test_settings_messaging.py "tests/test_telnyx_inbound.py::test_inbound_sms_text_is_length_capped" -q`
Expected: PASS (the length-cap test is unaffected — `to_number` defaults to `""`).

- [ ] **Step 6: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/src/usan_api/settings.py apps/api/src/usan_api/schemas/inbound_sms.py apps/api/tests/test_inbound_sms_to_number.py apps/api/tests/test_settings_messaging.py
git commit -m "feat(api): telnyx_inbound_sms_reply_enabled flag + parse inbound to_number (4b-2)"
```

---

### Task 4: Extract `generate_agent_reply` from `create_chat_completion`

**Files:**
- Modify: `apps/api/src/usan_api/compat/chat_service.py`
- Test: existing `apps/api/tests/compat/test_freeze_chats.py` (+ any `test_create_chat_completion.py`) — regression, must stay green

**Interfaces:**
- Produces: `generate_agent_reply(db, settings, session) -> str` (public, reused by Task 5). Behavior of `create_chat_completion` is unchanged.

- [ ] **Step 1: Add the shared helper**

In `apps/api/src/usan_api/compat/chat_service.py`, add (e.g. directly after `_load_published_config`):

```python
async def generate_agent_reply(
    db: AsyncSession, settings: Settings, session: ChatSession
) -> str:
    """Load the published config, build the system prompt + multi-turn contents from the
    FULL message history, run ONE text-only Vertex turn, return the reply text. The caller
    must have already persisted+flushed the latest user/sms turn so it appears in history.
    Raises on Vertex failure (the caller owns rollback). The role map sends "agent" turns as
    genai "model" and every other role ("user"/"sms") as "user"."""
    cfg = await _load_published_config(db, session.agent_profile_id)
    bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
    values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)
    history = await chats_repo.list_messages(db, session.id)
    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in history
    ]
    turn = await run_vertex_turn(
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        system_instruction=system_instruction,
        tools=[],
        contents=contents,
        settings=settings,
    )
    return turn.text
```

- [ ] **Step 2: Refactor `create_chat_completion` to use it**

In `create_chat_completion`, replace steps 5–7 (the `cfg = ...` through the `turn = await run_vertex_turn(...)` block, lines ~199–218) inside the `try` with:

```python
        # 5) one text-only Vertex turn over the full history (shared with the 4b-2 sms path)
        turn_text = await generate_agent_reply(db, settings, session)
```

and update step 8 to use `turn_text`:

```python
    # 8) persist the agent turn, commit, return ONLY the new agent message(s)
    agent_seq = await chats_repo.next_seq(db, session.id)
    agent_msg = await chats_repo.add_message(
        db, session_id=session.id, seq=agent_seq, role="agent", content=turn_text
    )
    await db.commit()
    return [agent_msg]
```

(The user turn is still persisted + flushed in step 4 before `generate_agent_reply`, so it appears in `history`. No behavior change.)

- [ ] **Step 3: Run the regression suite**

Run: `cd apps/api && uv run pytest -n0 -k "chat" tests/compat -q`
Expected: PASS (identical behavior; `mock_vertex` patches `chat_service.run_vertex_turn`, which `generate_agent_reply` still calls).

- [ ] **Step 4: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/src/usan_api/compat/chat_service.py
git commit -m "refactor(api): extract generate_agent_reply shared by chat completion + sms reply (4b-2)"
```

---

### Task 5: `compat/sms_reply.py` + wire into the Telnyx webhook

**Files:**
- Create: `apps/api/src/usan_api/compat/sms_reply.py`
- Modify: `apps/api/src/usan_api/routers/webhooks.py`
- Test: `apps/api/tests/test_inbound_sms_reply.py` (create)

**Interfaces:**
- Consumes: `find_open_sms_chat`, `add_message(provider_message_id=...)` (Task 2); `generate_agent_reply`, `_sms_send_ready` (Task 4 / existing); `to_e164`; `telnyx_messaging.send_sms`; `WEBHOOKS_TOTAL`; `Settings.telnyx_inbound_sms_reply_enabled` + `gcp_project`.
- Produces: `handle_inbound_sms_reply(db, settings, inbound) -> bool`; webhook branch ordered opt-out → sms-reply → family-task.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_inbound_sms_reply.py`:

```python
"""Inbound two-way SMS reply engine via POST /webhooks/telnyx (Phase 4b-2)."""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import telnyx_messaging
from usan_api.compat import sms_reply
from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings

_OUR = "+15550000000"
_RECIP = "+15551234567"


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "test-key")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "test-profile")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", _OUR)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    get_settings.cache_clear()

    def _sign(raw: bytes, ts: str) -> str:
        return base64.b64encode(priv.sign(f"{ts}|".encode() + raw)).decode()

    return _sign


@pytest.fixture
def disabled_signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.delenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", raising=False)
    get_settings.cache_clear()

    def _sign(raw: bytes, ts: str) -> str:
        return base64.b64encode(priv.sign(f"{ts}|".encode() + raw)).decode()

    return _sign


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake_reply(monkeypatch):
    """Patch the shared Vertex helper where the engine looks it up (no real Vertex / config)."""

    async def _gen(db, settings, session):
        return "Thanks, noted!"

    monkeypatch.setattr(sms_reply, "generate_agent_reply", _gen)


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number: str, body: str) -> str:
        calls.append({"to_number": to_number, "body": body})
        return "tx-out"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _send)
    return calls


def _envelope(message_id, text_body, *, sender, recipient):
    return json.dumps(
        {
            "data": {
                "event_type": "message.received",
                "id": f"evt_{message_id}",
                "payload": {
                    "id": message_id,
                    "from": {"phone_number": sender},
                    "to": [{"phone_number": recipient}],
                    "text": text_body,
                },
            }
        }
    ).encode()


def _post(client, signer, raw, *, ts=None):
    ts = ts or str(int(time.time()))
    return client.post(
        "/webhooks/telnyx",
        content=raw,
        headers={
            "telnyx-signature-ed25519": signer(raw, ts),
            "telnyx-timestamp": ts,
            "Content-Type": "application/json",
        },
    )


async def _seed_open_sms_chat(factory, *, frm=_OUR, to=_RECIP) -> uuid.UUID:
    async with factory() as db:
        profile = AgentProfile(
            name=f"A {uuid.uuid4().hex[:8]}",
            draft_config={"general_prompt": "x"},
            status=ProfileStatus.ACTIVE,
            published_version=1,
        )
        db.add(profile)
        await db.flush()
        s = ChatSession(
            agent_profile_id=profile.id, agent_version=1, chat_type="sms_chat",
            dynamic_vars={}, from_number=frm, to_number=to, status=ChatStatus.ONGOING,
        )
        db.add(s)
        await db.commit()
        return s.id


async def _messages(factory, session_id):
    async with factory() as db:
        return await chats_repo.list_messages(db, session_id)


@pytest.mark.asyncio
async def test_matched_inbound_generates_and_sends_reply(client, signer, fake_reply, recorded_sms, session_factory):
    sid = await _seed_open_sms_chat(session_factory)
    r = _post(client, signer, _envelope("m1", "hi back", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert len(recorded_sms) == 1
    assert recorded_sms[0]["to_number"] == _RECIP
    assert recorded_sms[0]["body"] == "Thanks, noted!"
    msgs = await _messages(session_factory, sid)
    roles = [(m.role, m.provider_message_id) for m in msgs]
    assert ("sms", "m1") in roles
    assert any(role == "agent" and pid is None for role, pid in roles)


@pytest.mark.asyncio
async def test_redelivery_is_deduped(client, signer, fake_reply, recorded_sms, session_factory):
    await _seed_open_sms_chat(session_factory)
    raw = _envelope("dup1", "hi", sender=_RECIP, recipient=_OUR)
    assert _post(client, signer, raw).status_code == 200
    assert _post(client, signer, raw).status_code == 200  # redelivery
    assert len(recorded_sms) == 1  # only one reply sent


@pytest.mark.asyncio
async def test_no_session_falls_through(client, signer, fake_reply, recorded_sms, session_factory):
    # no open sms_chat seeded -> engine returns False -> family-task path (no match) -> 200, no reply
    r = _post(client, signer, _envelope("m2", "hello", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []


@pytest.mark.asyncio
async def test_disabled_flag_does_not_reply(client, disabled_signer, recorded_sms, session_factory):
    await _seed_open_sms_chat(session_factory)
    r = _post(client, disabled_signer, _envelope("m3", "hi", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # flag off -> no reply


@pytest.mark.asyncio
async def test_send_failure_rolls_back_no_orphan(client, signer, fake_reply, monkeypatch, session_factory):
    sid = await _seed_open_sms_chat(session_factory)

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("boom")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)
    r = _post(client, signer, _envelope("m4", "hi", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    msgs = await _messages(session_factory, sid)
    assert all(m.provider_message_id != "m4" for m in msgs)  # inbound rolled back
    assert all(m.role != "agent" for m in msgs)  # no reply persisted


@pytest.mark.asyncio
async def test_stop_keyword_still_opts_out_not_replies(client, signer, fake_reply, recorded_sms, session_factory):
    await _seed_open_sms_chat(session_factory)
    r = _post(client, signer, _envelope("m5", "STOP", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # opt-out wins; no chat reply
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_inbound_sms_reply.py -q`
Expected: FAIL — `usan_api.compat.sms_reply` does not exist.

- [ ] **Step 3: Create the reply engine**

Create `apps/api/src/usan_api/compat/sms_reply.py`:

```python
"""Inbound two-way SMS reply engine (Phase 4b-2).

A matched-and-owned inbound reply: find the open sms_chat for an inbound message, persist
the recipient turn as role="sms" with the Telnyx message id (dedup), generate one Vertex
reply via the shared chat path, persist it as role="agent", and send it back. Inert behind
telnyx_inbound_sms_reply_enabled. PHI/secret-safe: logs only message_id + type(exc).__name__.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import telnyx_messaging
from usan_api.compat.chat_service import _sms_send_ready, generate_agent_reply
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
from usan_api.phone import to_e164
from usan_api.repositories import chats as chats_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import Settings


def _count(outcome: str) -> None:
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()


async def handle_inbound_sms_reply(
    db: AsyncSession, settings: Settings, inbound: InboundSms
) -> bool:
    """Drive one agent reply turn for an inbound SMS. Returns True iff the engine OWNS the
    message (an open sms_chat matched) — in which case the caller must NOT also route it to
    family-task intake. Returns False for flag-off / empty to_number / no matching session."""
    if not settings.telnyx_inbound_sms_reply_enabled or not inbound.to_number:
        return False
    our_number = to_e164(inbound.to_number) or inbound.to_number
    recipient = to_e164(inbound.from_number) or inbound.from_number
    session = await chats_repo.find_open_sms_chat(db, our_number=our_number, recipient=recipient)
    if session is None:
        return False

    # Matched: the sender is an sms_chat participant — we own the message from here, even if
    # we cannot reply (never relay a chat participant into family-task intake).
    if not _sms_send_ready(settings) or not settings.gcp_project:
        logger.bind(message_id=inbound.message_id).warning(
            "inbound sms reply skipped: messaging/Vertex not configured"
        )
        _count("sms_reply_unconfigured")
        return True

    # Persist the inbound turn first (role="sms"); the Telnyx message id dedups redeliveries.
    try:
        seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db,
            session_id=session.id,
            seq=seq,
            role="sms",
            content=inbound.text,
            provider_message_id=inbound.message_id,
        )
        await db.flush()
    except IntegrityError:
        await db.rollback()
        _count("sms_reply_dedup")
        return True

    # Generate + persist + send the reply; ANY failure discards the whole txn (no orphan PHI).
    try:
        reply = await generate_agent_reply(db, settings, session)
        reply_seq = await chats_repo.next_seq(db, session.id)
        await chats_repo.add_message(
            db, session_id=session.id, seq=reply_seq, role="agent", content=reply
        )
        await db.flush()
        await telnyx_messaging.send_sms(settings, to_number=recipient, body=reply)
    except Exception as exc:
        await db.rollback()
        logger.bind(message_id=inbound.message_id, err=type(exc).__name__).error(
            "inbound sms reply failed"
        )
        _count("sms_reply_failed")
        return True

    # Commit OUTSIDE the send try: wrapping it would mislabel a commit-fail as a send-fail
    # (the reply already went out) and risk a double-send on retry.
    await db.commit()
    _count("sms_reply")
    return True
```

- [ ] **Step 4: Wire it into the webhook**

In `apps/api/src/usan_api/routers/webhooks.py`, add the import (near the other `usan_api` imports):

```python
from usan_api.compat import sms_reply
```

In `telnyx_webhook`, insert the reply branch between the opt-out branch and the family-task call:

```python
    if telnyx_inbound.is_opt_out_keyword(inbound.text):
        await _route_inbound_opt_out(db, inbound)
        WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="opt_out").inc()
        return {"ok": True}
    if await sms_reply.handle_inbound_sms_reply(db, settings, inbound):
        # The reply engine owns the message (and increments its own metric).
        return {"ok": True}
    await _route_inbound_family_task(db, inbound)
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome="ok").inc()
    return {"ok": True}
```

- [ ] **Step 5: Run to verify it passes + the existing webhook contract holds**

Run: `cd apps/api && uv run pytest -n0 tests/test_inbound_sms_reply.py tests/test_telnyx_inbound.py tests/test_inbound_stop.py -q`
Expected: PASS (new engine works; opt-out, family-task, signature 401, replay 401, bad JSON 400 all unchanged — for those payloads the engine returns False because no open sms_chat is seeded).

- [ ] **Step 6: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/src/usan_api/compat/sms_reply.py apps/api/src/usan_api/routers/webhooks.py apps/api/tests/test_inbound_sms_reply.py
git commit -m "feat(api): inbound two-way SMS reply engine wired into /webhooks/telnyx (4b-2)"
```

---

### Task 6: Conformance freeze (role 'sms' two-way) + operator doc

**Files:**
- Modify: `apps/api/tests/compat/test_freeze_sms_chat.py`
- Modify: `docs/deployment/sms-chat.md`

**Interfaces:**
- Consumes: the serializer's pass-through of `role` (Phase 4a) + the conformance helpers.

- [ ] **Step 1: Write the failing freeze test**

Append to `apps/api/tests/compat/test_freeze_sms_chat.py` (if `json` is already imported at the top of the file, drop the duplicate import):

```python
from sqlalchemy import text as _sa_text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _append_two_way(dsn: str, chat_id_uuid: str) -> None:
    """Insert an inbound role='sms' turn + an agent reply for an existing session, so get-chat
    serializes a two-way sms_chat (the 4b-2 reply-engine end state) for conformance checks."""
    import asyncio

    async def _run() -> None:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                nxt = (
                    await conn.execute(
                        _sa_text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages "
                            "WHERE chat_session_id = :sid"
                        ),
                        {"sid": chat_id_uuid},
                    )
                ).scalar_one()
                await conn.execute(
                    _sa_text(
                        "INSERT INTO chat_messages (chat_session_id, seq, role, content, provider_message_id) "
                        "VALUES (:sid, :seq, 'sms', 'hi back', 'tx-in-1')"
                    ),
                    {"sid": chat_id_uuid, "seq": nxt},
                )
                await conn.execute(
                    _sa_text(
                        "INSERT INTO chat_messages (chat_session_id, seq, role, content) "
                        "VALUES (:sid, :seq, 'agent', 'Glad to hear it!')"
                    ),
                    {"sid": chat_id_uuid, "seq": nxt + 1},
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_two_way_sms_chat_conforms_with_sms_role(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms, async_database_url
):
    from usan_api.compat import ids

    chat_id = _create_sms(compat_client, compat_headers, web_agent_id).json()["chat_id"]
    _append_two_way(async_database_url, str(ids.decode_chat_id(chat_id)))

    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers).json()
    roles = [m["role"] for m in got["message_with_tool_calls"]]
    assert "sms" in roles  # oracle-faithful inbound role
    assert "agent" in roles
    assert_conforms(got, "ChatResponse")
    assert_sdk_roundtrip(got, "retell.types:ChatResponse")
```

- [ ] **Step 2: Run to verify it passes**

Run: `cd apps/api && uv run pytest -n0 "tests/compat/test_freeze_sms_chat.py::test_two_way_sms_chat_conforms_with_sms_role" -q`
Expected: PASS once Tasks 1–5 are merged (the column + serializer already pass `role` through; this guards that `role="sms"` conforms to the oracle `SmsMessage` variant and SDK-round-trips).

- [ ] **Step 3: Update the operator doc**

Append an "Inbound two-way replies (Phase 4b-2)" section to `docs/deployment/sms-chat.md` documenting: the new `TELNYX_INBOUND_SMS_REPLY_ENABLED` flag (default off); that a live reply also requires `TELNYX_MESSAGING_ENABLED` + the three messaging secrets, `GCP_PROJECT` (Vertex), and `TELNYX_INBOUND_PUBLIC_KEY` (signature); that migration `0044` must be applied; that the webhook always acks 200 (failures recorded via `WEBHOOKS_TOTAL{type="telnyx_sms"}` outcomes `sms_reply` / `sms_reply_dedup` / `sms_reply_unconfigured` / `sms_reply_failed`); and that unknown-recipient auto-create is deferred (4b-3).

- [ ] **Step 4: Lint + commit**

Run: `cd apps/api && uv run mypy && ruff check . && ruff format .`

```bash
git add apps/api/tests/compat/test_freeze_sms_chat.py docs/deployment/sms-chat.md
git commit -m "test(api): two-way sms_chat role='sms' conformance freeze + operator doc (4b-2)"
```

---

## Final verification (after all tasks)

- [ ] `cd apps/api && uv run pytest` (full suite, parallel) — green.
- [ ] `cd apps/api && uv run mypy && ruff check . && ruff format --check .` — clean.
- [ ] `cd apps/api && uv run alembic heads` — exactly one head `0044`.
- [ ] Whole-branch review, then finish-branch (push + PR; squash-merge on the user's explicit go-ahead; **no `v*` tag**).

## Self-review notes

- **Spec coverage:** §3 scope → Tasks 1–6; §5 components → Tasks 1–5; §6 number-mapping → Task 2 matcher + Task 5 envelope; §7 dedup → Tasks 1/2/5; §8 error semantics → Task 5 tests; §9 inert flag → Task 3; §10 PHI → Task 5 engine; §11 testing → Tasks 1–6.
- **Type consistency:** `handle_inbound_sms_reply(db, settings, inbound) -> bool`; `find_open_sms_chat(db, *, our_number, recipient) -> ChatSession | None`; `generate_agent_reply(db, settings, session) -> str`; `add_message(..., provider_message_id: str | None = None)` — names/signatures match across tasks.
- **No mixing of `client` + `compat_client`** in one test (both truncate on setup): the webhook engine tests seed via a superuser `session_factory` and drive `client`; the conformance freeze uses `compat_client` + a raw-SQL two-way append.
- **`generate_agent_reply` extraction** is behavior-preserving — covered by the unchanged 4a chat-completion tests (Task 4 regression), so the engine tests can patch it at the `sms_reply` boundary without losing coverage of the real Vertex path.
