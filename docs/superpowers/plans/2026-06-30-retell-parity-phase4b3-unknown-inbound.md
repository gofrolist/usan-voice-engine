# Phase 4b-3 — Unknown-Recipient Inbound SMS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an inbound SMS arrives at a provisioned DID that carries an `inbound_sms_agents` binding and matches no open chat, auto-create an `sms_chat` and run one agent reply turn — instead of today's silent 200-noop.

**Architecture:** A new gated, best-effort handler `compat/inbound_autocreate.handle_inbound_autocreate` is inserted into the existing `POST /webhooks/telnyx` flow, between the 4b-2 reply engine and the family-task fall-through. It resolves the destination DID's first-bound inbound SMS agent, creates an `sms_chat` oriented exactly like an outbound-originated one (`from=our DID, to=sender, ONGOING`), persists the inbound turn `role='sms'` (the dedup point), runs `generate_agent_reply`, sends the reply, and commits — mirroring the 4b-2 reply engine's transaction/ownership/dedup/commit-outside-send shape. Single-org (default org via RLS), no migration, ships inert.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, Pydantic settings, loguru, pytest (`-n auto`), ruff, mypy. apps/api only.

## Global Constraints

_Every task's requirements implicitly include this section. Values are copied verbatim from the spec._

- **apps/api only** — `services/agent` is untouched; no cross-service import.
- **PHI/secret-safe logging only** — log `message_id` + `type(exc).__name__`; if a phone must ever appear in a log, mask it with `mask_phone(...)`. NEVER log inbound text, the agent reply, the resolved `agent_id`, or a raw E.164.
- **`organization_id` is server-set by RLS** (the seeded default org), never by app code.
- **NO new alembic migration** — rides existing `chat_sessions`/`chat_messages` + `phone_numbers`; single head stays `0047`.
- **NO new served operation** → `KNOWN_GAPS = frozenset()` stays empty and `tests/compat/test_surface_coverage.py` stays green.
- **Ships INERT** — `telnyx_inbound_sms_autocreate_enabled` defaults False; NO `v*` tag cut by this phase.
- **`exclude_none` preserved** — this path adds no new HTTP response body.
- **CI mypy** = `uv run mypy` with config `files=["src"]` — NEVER `mypy .` (run from `apps/api`).
- **ruff** line-length 100, target py314 (apps/api).
- This env's text display strips parens from `except (A, B):` — verify syntax via `python -m py_compile`, not by eye.
- **apps/api pytest runs `-n auto`** — RLS/global-state tests tolerate sibling rows AND run on the non-superuser `app_session` fixture (CI's `usan` superuser BYPASSES RLS, so RLS-meaningful assertions MUST use `app_session`).
- **Never raise out of the webhook** — always 200; Telnyx idempotency comes from the unique index, not from non-2xx retries.
- **STOP/opt-out precedence stays FIRST** (unchanged); known family contacts must still reach `_route_inbound_family_task` (the Gate 2 guard).
- Commit format `type(scope): description`, scope `api`/`docs`. Attribution disabled globally (no `Co-Authored-By`, no footer).

---

## File Structure

- **Create** `apps/api/src/usan_api/compat/inbound_autocreate.py` — the picker + the handler. One responsibility: the unknown-recipient inbound SMS auto-create path.
- **Modify** `apps/api/src/usan_api/settings.py` — one inert flag beside `telnyx_inbound_sms_reply_enabled`.
- **Modify** `apps/api/src/usan_api/routers/webhooks.py` — import + a 2-line insertion between the reply engine and family-task.
- **Create** `apps/api/tests/compat/test_inbound_autocreate.py` — picker unit tests + direct-call handler tests (`app_session`).
- **Create** `apps/api/tests/test_inbound_sms_autocreate_webhook.py` — webhook-level wiring/precedence tests (signed `POST /webhooks/telnyx`).
- **Modify** `apps/api/tests/test_settings_messaging.py` — the flag default/env tests.
- **Create** `docs/deployment/inbound-sms-autocreate.md` — operator activation note.

The auto-created chat surfaces through the already-served get-chat / list-chats — **no router/schema change**.

---

## Task 1: Inert feature flag

**Files:**
- Modify: `apps/api/src/usan_api/settings.py:157-161` (beside `telnyx_inbound_sms_reply_enabled`)
- Test: `apps/api/tests/test_settings_messaging.py`

**Interfaces:**
- Produces: `Settings.telnyx_inbound_sms_autocreate_enabled: bool` (default `False`, alias `TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED`).

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_settings_messaging.py` (mirrors the existing `telnyx_inbound_sms_reply_enabled` tests at lines 71-88; reuses the file's `_set_required_env`):

```python
def test_inbound_sms_autocreate_disabled_by_default(monkeypatch):
    from usan_api.settings import get_settings

    _set_required_env(monkeypatch)
    monkeypatch.delenv("TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().telnyx_inbound_sms_autocreate_enabled is False
    get_settings.cache_clear()


def test_inbound_sms_autocreate_enabled_via_env(monkeypatch):
    from usan_api.settings import get_settings

    _set_required_env(monkeypatch)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().telnyx_inbound_sms_autocreate_enabled is True
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_settings_messaging.py -k autocreate -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'telnyx_inbound_sms_autocreate_enabled'`.

- [ ] **Step 3: Add the field** — in `settings.py`, immediately after the `telnyx_inbound_sms_reply_enabled` Field (line 161):

```python
    # Phase 4b-3: gate the unknown-recipient inbound SMS auto-create path independently of
    # the reply engine, so it can be staged/rolled-back on its own. Default FALSE.
    telnyx_inbound_sms_autocreate_enabled: bool = Field(
        default=False, alias="TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED"
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_settings_messaging.py -k autocreate -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/api && uv run ruff check src/usan_api/settings.py tests/test_settings_messaging.py && uv run ruff format src/usan_api/settings.py tests/test_settings_messaging.py
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings_messaging.py
git commit -m "feat(api): add telnyx_inbound_sms_autocreate_enabled flag (4b-3, inert)"
```

---

## Task 2: `_pick_inbound_sms_agent` picker (new module)

Creates `compat/inbound_autocreate.py` with the pure picker + the `_count` metric helper. The handler is added in Task 3.

**Files:**
- Create: `apps/api/src/usan_api/compat/inbound_autocreate.py`
- Test: `apps/api/tests/compat/test_inbound_autocreate.py`

**Interfaces:**
- Produces: `_pick_inbound_sms_agent(pn: PhoneNumber | None) -> str | None` — the first-entry `agent_id` token from `pn.inbound_sms_agents`, else `None`. Mirrors `_resolve_sms_agent`'s outbound extraction (`chat_service.py:90-95`). Our schema stores ONLY the list — the oracle's `inbound_sms_agent_id` scalar is collapsed into it by the Phase 2 CRUD, so there is no separate scalar to fall back to.
- Produces: `_count(outcome: str) -> None` — `WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()` (identical to `sms_reply._count`).

- [ ] **Step 1: Write the failing unit tests** — create `apps/api/tests/compat/test_inbound_autocreate.py`:

```python
"""Unknown-recipient inbound SMS auto-create (Phase 4b-3)."""

from __future__ import annotations

from usan_api.compat.inbound_autocreate import _pick_inbound_sms_agent
from usan_api.db.models import PhoneNumber


def _pn(inbound_sms_agents):
    return PhoneNumber(
        phone_e164="+15550000000",
        phone_number_type="custom",
        inbound_sms_agents=inbound_sms_agents,
    )


def test_pick_first_entry():
    pn = _pn([{"agent_id": "agent_aaa", "weight": 1.0}, {"agent_id": "agent_bbb"}])
    assert _pick_inbound_sms_agent(pn) == "agent_aaa"


def test_pick_none_phone_number():
    assert _pick_inbound_sms_agent(None) is None


def test_pick_empty_binding():
    assert _pick_inbound_sms_agent(_pn(None)) is None
    assert _pick_inbound_sms_agent(_pn([])) is None


def test_pick_malformed_entry():
    assert _pick_inbound_sms_agent(_pn([{"weight": 1.0}])) is None  # no agent_id
    assert _pick_inbound_sms_agent(_pn([{"agent_id": ""}])) is None  # blank
    assert _pick_inbound_sms_agent(_pn([{"agent_id": 123}])) is None  # non-str
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_inbound_autocreate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.compat.inbound_autocreate'`.

- [ ] **Step 3: Create the module with the picker + `_count`** — `apps/api/src/usan_api/compat/inbound_autocreate.py`:

```python
"""Unknown-recipient inbound SMS auto-create (Phase 4b-3).

When an inbound SMS arrives at a provisioned DID that carries an inbound_sms_agents
binding and matches no open sms_chat, auto-create an sms_chat oriented like an outbound
one (from=our DID, to=sender, ONGOING), persist the inbound turn role="sms" (the dedup
point), run one Vertex reply, persist it role="agent", and send it. Inert behind
telnyx_inbound_sms_autocreate_enabled. Single-org (RLS default org). PHI/secret-safe:
logs only message_id + type(exc).__name__ (never message text, reply, agent_id, or phone).
"""

from __future__ import annotations

from usan_api.db.models import PhoneNumber
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL


def _count(outcome: str) -> None:
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()


def _pick_inbound_sms_agent(pn: PhoneNumber | None) -> str | None:
    """First-entry agent_id token from the DID's inbound_sms_agents binding, else None.

    Deterministic first-entry mirrors _resolve_sms_agent's outbound[0] pick (chat_service.py)
    and is isolated here so a weighted-random pick can replace it without touching the caller.
    Our schema stores only the list (the oracle's inbound_sms_agent_id scalar is collapsed
    into it by the Phase 2 CRUD), so first-entry IS the scalar-equivalent.
    """
    agents = (pn.inbound_sms_agents if pn is not None else None) or []
    token = (agents[0] or {}).get("agent_id") if agents else None
    return token if isinstance(token, str) and token else None
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_inbound_autocreate.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check src/usan_api/compat/inbound_autocreate.py tests/compat/test_inbound_autocreate.py && uv run ruff format src/usan_api/compat/inbound_autocreate.py tests/compat/test_inbound_autocreate.py && uv run mypy
git add apps/api/src/usan_api/compat/inbound_autocreate.py apps/api/tests/compat/test_inbound_autocreate.py
git commit -m "feat(api): add inbound_sms_agents picker for 4b-3 auto-create"
```

---

## Task 3: `handle_inbound_autocreate` handler

Adds the gated, best-effort, single-transaction handler to `compat/inbound_autocreate.py` and its direct-call integration tests.

**Files:**
- Modify: `apps/api/src/usan_api/compat/inbound_autocreate.py`
- Test: `apps/api/tests/compat/test_inbound_autocreate.py` (append)

**Interfaces:**
- Consumes: `_pick_inbound_sms_agent`, `_count` (Task 2); `chats_repo.add_session/next_seq/add_message` (`repositories/chats.py`); `phone_numbers_repo.get_by_e164`; `family_contacts_repo.find_contacts_by_phone`; `agent_profiles_repo.get_profile`; `chat_service.generate_agent_reply` + `_sms_send_ready`; `telnyx_messaging.send_sms`; `ids.decode_agent_id`; `to_e164`.
- Produces: `async def handle_inbound_autocreate(db: AsyncSession, settings: Settings, inbound: InboundSms) -> bool` — returns `True` when it OWNS the message (created+replied, dedup, or messaging-unconfigured skip); `False` when a gate declines (flag off / empty to_number / no binding / non-live bound agent / known family contact) so the family-task fall-through runs.
- Produces: `async def _resolve_live_profile(db: AsyncSession, token: str) -> AgentProfile | None`.

- [ ] **Step 1: Write the failing direct-call tests** — append to `apps/api/tests/compat/test_inbound_autocreate.py`. (`app_session` is the non-superuser session from `tests/conftest.py`; the autouse `_compat_minimal_env` in `tests/compat/conftest.py` supplies env so `get_settings()` doesn't raise.)

```python
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from usan_api import telnyx_messaging
from usan_api.compat import ids, inbound_autocreate
from usan_api.compat.inbound_autocreate import handle_inbound_autocreate
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

_OUR = "+15550000000"
_SENDER = "+15551234567"


def _settings(**overrides):
    base = {
        "telnyx_inbound_sms_autocreate_enabled": True,
        "telnyx_messaging_enabled": True,
        "telnyx_messaging_api_key": SecretStr("k"),
        "telnyx_messaging_profile_id": "p",
        "telnyx_from_number": _OUR,
        "gcp_project": "proj",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _inbound(message_id="m1", *, sender=_SENDER, recipient=_OUR, text_body="hello"):
    return InboundSms(
        message_id=message_id,
        from_number=sender,
        to_number=recipient,
        text=text_body,
        event_type="message.received",
    )


async def _seed(db, *, bind=True, active=True):
    """Set tenant context, seed a (live) agent profile + a phone_number bound to it."""
    org_id = (await db.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(db, org_id)
    profile = AgentProfile(
        name=f"A {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "x"},
        status=ProfileStatus.ACTIVE if active else ProfileStatus.ARCHIVED,
        published_version=1 if active else None,
    )
    db.add(profile)
    await db.flush()
    agents = [{"agent_id": ids.encode_agent_id(profile.id), "weight": 1.0}] if bind else None
    db.add(PhoneNumber(phone_e164=_OUR, phone_number_type="custom", inbound_sms_agents=agents))
    await db.flush()
    return profile


@pytest.fixture
def fake_reply(monkeypatch):
    async def _gen(db, settings, session):
        return "Thanks, noted!"

    monkeypatch.setattr(inbound_autocreate, "generate_agent_reply", _gen)


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number, body):
        calls.append({"to_number": to_number, "body": body})
        return "tx-out"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _send)
    return calls


async def _count_sessions(db) -> int:
    return int((await db.execute(text("SELECT count(*) FROM chat_sessions"))).scalar_one())


@pytest.mark.asyncio
async def test_flag_off_is_noop(app_session, fake_reply, recorded_sms):
    await _seed(app_session)
    result = await handle_inbound_autocreate(
        app_session, _settings(telnyx_inbound_sms_autocreate_enabled=False), _inbound()
    )
    assert result is False
    assert recorded_sms == []
    assert await _count_sessions(app_session) == 0
    await app_session.rollback()


@pytest.mark.asyncio
async def test_bound_unknown_sender_creates_chat_and_replies(app_session, fake_reply, recorded_sms):
    await _seed(app_session)
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound("mA"))
    assert result is True
    assert await _count_sessions(app_session) == 1
    rows = (
        await app_session.execute(
            text("SELECT role, provider_message_id FROM chat_messages ORDER BY seq ASC")
        )
    ).all()
    assert [r[0] for r in rows] == ["sms", "agent"]
    assert rows[0][1] == "mA"  # inbound turn keyed by Telnyx id
    assert rows[1][1] is None  # agent reply has no provider id
    assert recorded_sms == [{"to_number": _SENDER, "body": "Thanks, noted!"}]
    await app_session.rollback()


@pytest.mark.asyncio
async def test_no_binding_declines(app_session, fake_reply, recorded_sms):
    await _seed(app_session, bind=False)  # phone_number exists but no inbound_sms_agents
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False
    assert recorded_sms == []
    assert await _count_sessions(app_session) == 0
    await app_session.rollback()


@pytest.mark.asyncio
async def test_non_live_bound_agent_declines(app_session, fake_reply, recorded_sms):
    await _seed(app_session, active=False)  # bound, but agent archived/unpublished
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False
    assert await _count_sessions(app_session) == 0
    await app_session.rollback()


@pytest.mark.asyncio
async def test_known_family_contact_declines(app_session, fake_reply, recorded_sms, monkeypatch):
    await _seed(app_session)

    async def _match(db, phone):
        return [object()]  # a known family contact for this sender

    monkeypatch.setattr(family_contacts_repo, "find_contacts_by_phone", _match)
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False  # caregiver relay not hijacked -> family-task runs
    assert await _count_sessions(app_session) == 0
    await app_session.rollback()


@pytest.mark.asyncio
async def test_unconfigured_owns_but_skips(app_session, fake_reply, recorded_sms):
    await _seed(app_session)
    result = await handle_inbound_autocreate(app_session, _settings(gcp_project=None), _inbound())
    assert result is True  # bound DID is SMS-agent territory -> owned, not relayed
    assert recorded_sms == []
    assert await _count_sessions(app_session) == 0  # no orphan session
    await app_session.rollback()


@pytest.mark.asyncio
async def test_duplicate_message_id_deduped(app_session, fake_reply, recorded_sms):
    await _seed(app_session)
    assert await handle_inbound_autocreate(app_session, _settings(), _inbound("dup")) is True
    # Same Telnyx id again (direct call bypasses the reply-engine matcher) -> dedup, no 2nd chat.
    assert await handle_inbound_autocreate(app_session, _settings(), _inbound("dup")) is True
    assert await _count_sessions(app_session) == 1
    assert len(recorded_sms) == 1
    await app_session.rollback()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_inbound_autocreate.py -q`
Expected: FAIL — `ImportError: cannot import name 'handle_inbound_autocreate'`.

- [ ] **Step 3: Implement the handler** — replace the imports block at the top of `compat/inbound_autocreate.py` with the full set, then add `_resolve_live_profile` + `handle_inbound_autocreate` below `_pick_inbound_sms_agent`.

Imports block (replaces the current `from __future__` + two imports):

```python
from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import telnyx_messaging
from usan_api.compat import ids
from usan_api.compat.chat_service import _sms_send_ready, generate_agent_reply
from usan_api.compat.errors import CompatError
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
from usan_api.phone import to_e164
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import chats as chats_repo
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import phone_numbers as phone_numbers_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import Settings
```

Handler + resolver (append after `_pick_inbound_sms_agent`):

```python
async def _resolve_live_profile(db: AsyncSession, token: str) -> AgentProfile | None:
    """The live published AgentProfile for a bound agent token, or None when the token is
    malformed / missing / not ACTIVE / unpublished. Channel-lenient like create_sms_chat
    (a voice agent bound to an SMS number is accepted)."""
    try:
        profile_id = ids.decode_agent_id(token)
    except CompatError:
        return None
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    if (
        profile is None
        or profile.status is not ProfileStatus.ACTIVE
        or profile.published_version is None
    ):
        return None
    return profile


async def handle_inbound_autocreate(
    db: AsyncSession, settings: Settings, inbound: InboundSms
) -> bool:
    """Auto-create an sms_chat for an inbound SMS to a bound DID and run one reply turn.

    Returns True iff the handler OWNS the message (created+replied, deduped, or
    messaging-unconfigured skip) — the caller must NOT then route it to family-task. Returns
    False when a gate declines (flag off / empty to_number / no usable binding / known family
    contact), so the family-task fall-through still runs.
    """
    if not settings.telnyx_inbound_sms_autocreate_enabled or not inbound.to_number:
        return False
    our_number = to_e164(inbound.to_number) or inbound.to_number
    recipient = to_e164(inbound.from_number) or inbound.from_number

    # Gate 1: the destination DID must carry an inbound_sms_agents binding.
    pn = await phone_numbers_repo.get_by_e164(db, our_number)
    token = _pick_inbound_sms_agent(pn)
    if token is None:
        return False

    # Gate 2: never hijack a known family contact's caregiver relay (FR-008/014) — let the
    # family-task fall-through handle them.
    if await family_contacts_repo.find_contacts_by_phone(db, recipient):
        return False

    # Gate 3: the bound agent must resolve to a live published profile (else no usable binding).
    profile = await _resolve_live_profile(db, token)
    if profile is None:
        return False

    # Gate 4: a bound DID is SMS-agent territory — own the message but skip if we cannot reply.
    if not _sms_send_ready(settings) or not settings.gcp_project:
        logger.bind(message_id=inbound.message_id).warning(
            "inbound sms autocreate skipped: messaging/Vertex not configured"
        )
        _count("sms_autocreate_unconfigured")
        return True

    # Create the session + persist the inbound turn (role="sms"); the Telnyx id dedups
    # redeliveries. Persisting in the SAME txn as the session means a concurrent/duplicate
    # first delivery serializes on uq_chat_messages_provider_msg — the loser's whole txn
    # (session + message) rolls back.
    try:
        session = await chats_repo.add_session(
            db,
            agent_profile_id=profile.id,
            agent_version=profile.published_version,
            dynamic_vars={},
            chat_type="sms_chat",
            from_number=our_number,
            to_number=recipient,
        )
        await db.flush()
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
        _count("sms_autocreate_dedup")
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
            "inbound sms autocreate failed"
        )
        _count("sms_autocreate_failed")
        return True

    # Commit OUTSIDE the send try: wrapping it would mislabel a commit-fail as a send-fail
    # (the reply already went out) and risk a double-send on retry (mirrors sms_reply.py).
    await db.commit()
    _count("sms_autocreate")
    return True
```

> **PHI rule for the reviewer:** the log lines bind only `message_id` — never a phone, the message text, the reply, or the resolved `agent_id`. This satisfies the "no raw phone in any log" constraint without needing `mask_phone` here. If a future log line must include a phone, import and use `usan_api.masking.mask_phone`.

- [ ] **Step 4: Verify syntax + run the tests**

Run: `cd apps/api && python -m py_compile src/usan_api/compat/inbound_autocreate.py && uv run pytest -n0 tests/compat/test_inbound_autocreate.py -q`
Expected: PASS (all picker + handler tests; ~11 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check src/usan_api/compat/inbound_autocreate.py tests/compat/test_inbound_autocreate.py && uv run ruff format src/usan_api/compat/inbound_autocreate.py tests/compat/test_inbound_autocreate.py && uv run mypy
git add apps/api/src/usan_api/compat/inbound_autocreate.py apps/api/tests/compat/test_inbound_autocreate.py
git commit -m "feat(api): 4b-3 unknown-recipient inbound SMS auto-create handler"
```

---

## Task 4: Wire into the Telnyx webhook

**Files:**
- Modify: `apps/api/src/usan_api/routers/webhooks.py:9` (import) and `:143-146` (insertion)
- Test: `apps/api/tests/test_inbound_sms_autocreate_webhook.py` (new)

**Interfaces:**
- Consumes: `inbound_autocreate.handle_inbound_autocreate` (Task 3).
- Produces: webhook order `reply → autocreate → family-task`; the auto-create handler owns the message (returns 200) when it fires.

- [ ] **Step 1: Write the failing webhook tests** — create `apps/api/tests/test_inbound_sms_autocreate_webhook.py` (mirrors `tests/test_inbound_sms_reply.py`'s signer/envelope/post/session_factory helpers — copy them, adding the autocreate flag to the signer env):

```python
"""Unknown-recipient inbound SMS auto-create via POST /webhooks/telnyx (Phase 4b-3)."""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import telnyx_messaging
from usan_api.compat import ids, inbound_autocreate
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.settings import get_settings

_OUR = "+15550000000"
_SENDER = "+15551234567"


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED", "true")
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
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake_reply(monkeypatch):
    async def _gen(db, settings, session):
        return "Thanks, noted!"

    monkeypatch.setattr(inbound_autocreate, "generate_agent_reply", _gen)


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number, body):
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


async def _seed_bound_number(factory) -> None:
    async with factory() as db:
        profile = AgentProfile(
            name=f"A {uuid.uuid4().hex[:8]}",
            draft_config={"general_prompt": "x"},
            status=ProfileStatus.ACTIVE,
            published_version=1,
        )
        db.add(profile)
        await db.flush()
        db.add(
            PhoneNumber(
                phone_e164=_OUR,
                phone_number_type="custom",
                inbound_sms_agents=[{"agent_id": ids.encode_agent_id(profile.id), "weight": 1.0}],
            )
        )
        await db.commit()


async def _session_count(factory) -> int:
    async with factory() as db:
        return int((await db.execute(text("SELECT count(*) FROM chat_sessions"))).scalar_one())


@pytest.mark.asyncio
async def test_unknown_sender_autocreates_and_replies(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    r = _post(client, signer, _envelope("m1", "hi", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == [{"to_number": _SENDER, "body": "Thanks, noted!"}]
    assert await _session_count(session_factory) == 1


@pytest.mark.asyncio
async def test_stop_keyword_wins_over_autocreate(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    r = _post(client, signer, _envelope("m2", "STOP", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # opt-out first; no chat
    assert await _session_count(session_factory) == 0


@pytest.mark.asyncio
async def test_no_binding_falls_through_to_family_task(
    client, signer, fake_reply, recorded_sms, session_factory
):
    # No phone_number seeded -> Gate 1 declines -> family-task path (unmatched) -> 200, no chat.
    r = _post(client, signer, _envelope("m3", "hello", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []
    assert await _session_count(session_factory) == 0


@pytest.mark.asyncio
async def test_redelivery_deduped_one_chat(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    raw = _envelope("dup", "hi", sender=_SENDER, recipient=_OUR)
    assert _post(client, signer, raw).status_code == 200
    # Redelivery: the now-open sms_chat matches the 4b-2 reply engine (dedup), not autocreate.
    assert _post(client, signer, raw).status_code == 200
    assert await _session_count(session_factory) == 1
    assert len(recorded_sms) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_inbound_sms_autocreate_webhook.py -q`
Expected: FAIL — `test_unknown_sender_autocreates_and_replies` and `test_redelivery_deduped_one_chat` fail (no chat created / no reply) because the webhook does not yet call the handler.

- [ ] **Step 3: Wire the handler into the webhook** — in `routers/webhooks.py`, change the import on line 9 and insert the call.

Change line 9 from `from usan_api.compat import sms_reply` to:

```python
from usan_api.compat import inbound_autocreate, sms_reply
```

Then in `telnyx_webhook`, between the reply-engine block (`:143-145`) and `_route_inbound_family_task` (`:146`), insert the autocreate call so the block reads:

```python
    if await sms_reply.handle_inbound_sms_reply(db, settings, inbound):
        # The reply engine owns the message (and increments its own metric).
        return {"ok": True}
    if await inbound_autocreate.handle_inbound_autocreate(db, settings, inbound):
        # Auto-create owns an unknown-recipient inbound to a bound DID (own metric).
        return {"ok": True}
    await _route_inbound_family_task(db, inbound)
```

- [ ] **Step 4: Verify syntax + run the new + existing webhook tests**

Run: `cd apps/api && python -m py_compile src/usan_api/routers/webhooks.py && uv run pytest -n0 tests/test_inbound_sms_autocreate_webhook.py tests/test_inbound_sms_reply.py tests/test_inbound_stop.py tests/test_family_tasks.py -q`
Expected: PASS (the new file passes; the 4b-2 reply, STOP, and family-task suites still pass — precedence unchanged).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && uv run ruff check src/usan_api/routers/webhooks.py tests/test_inbound_sms_autocreate_webhook.py && uv run ruff format src/usan_api/routers/webhooks.py tests/test_inbound_sms_autocreate_webhook.py && uv run mypy
git add apps/api/src/usan_api/routers/webhooks.py apps/api/tests/test_inbound_sms_autocreate_webhook.py
git commit -m "feat(api): wire 4b-3 inbound SMS auto-create into the Telnyx webhook"
```

---

## Task 5: Operator deployment note

**Files:**
- Create: `docs/deployment/inbound-sms-autocreate.md`

- [ ] **Step 1: Write the note** — `docs/deployment/inbound-sms-autocreate.md`:

```markdown
# Inbound SMS Auto-Create (Phase 4b-3)

When an inbound SMS arrives at a provisioned DID that carries an `inbound_sms_agents`
binding and matches no open chat, the system auto-creates an `sms_chat` and runs one agent
reply turn. The created chat is retrievable via the existing get-chat / list-chats — there
is no new endpoint.

## Ships inert

Disabled by default. No behavior changes until activated. No `v*` tag is cut by this phase.

## Activation order

1. Provision the inbound DID with an `inbound_sms_agents` binding (the first entry's
   `agent_id` is used; the agent must be a published, ACTIVE profile).
2. Ensure the reply path is configured: `TELNYX_MESSAGING_ENABLED=true` + the three Telnyx
   messaging secrets + `GCP_PROJECT` (Vertex). Without these the handler still OWNS a bound
   DID's inbound but logs `sms_autocreate_unconfigured` and creates nothing.
3. Set `TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED=true` in BOTH the compose `api` `environment:`
   map AND the VM `.env` (the compose-env-passthrough two-place rule), then redeploy/reboot
   so the VM `.env` is refreshed from Secret Manager.

It is independent of `TELNYX_INBOUND_SMS_REPLY_ENABLED` so it can be staged/rolled back on
its own.

## Precedence

STOP/opt-out is honored first (a STOP from anyone is never auto-created). An open
outbound-originated `sms_chat` is handled by the 4b-2 reply engine. A known family contact
falls through to family-task intake (the caregiver relay is never hijacked). Auto-create
fires only for an unknown sender to a bound DID.

## Security / PHI

`organization_id` is server-set by RLS (the seeded default org). Logs carry only the Telnyx
`message_id` + exception type names — never message text, the reply, the agent id, or a raw
phone number. Metrics: `WEBHOOKS_TOTAL{type="telnyx_sms", outcome=...}` with outcomes
`sms_autocreate` / `sms_autocreate_dedup` / `sms_autocreate_unconfigured` /
`sms_autocreate_failed`.

## Known limitation

Single-org only. An inbound DID is attributed to the seeded default org; cross-org DID->org
routing (a `SECURITY DEFINER` DID lookup) is deferred to a future phase, to be built when a
second org has live inbound SMS.
```

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/inbound-sms-autocreate.md
git commit -m "docs(api): operator note for 4b-3 inbound SMS auto-create"
```

---

## Final verification (after all tasks)

```bash
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

Expected: full apps/api suite green; ruff + mypy clean; single alembic head `0047`;
`tests/compat/test_surface_coverage.py` green (`KNOWN_GAPS` unchanged).
