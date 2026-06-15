"""T044 (US4): personal memory built-ins are carried into the next call.

The four memory built-ins (``personal_facts``, ``last_call_summary``, ``open_plans``,
``important_dates``) are resolved API-side from the contact's active ``personal_facts`` and
their most recent ``conversation_summaries`` row, then carried OUT-OF-BAND in the
``resolved_vars`` the call-create path hands to ``dispatch_agent`` — the same carry
channel as ``open_family_tasks`` / ``pending_med_reasks``. Both the outbound enqueue and
the inbound resolver are hand-written carry sites, so both are asserted.

Written FIRST (Constitution IV) — fails until the repos + builtin wiring land.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def dispatch_spy(monkeypatch):
    from usan_api import dialer

    spy = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", spy)
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
    return spy


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _worker_auth(secret: str = "s" * 32) -> dict:
    now = int(time.time())
    token = jwt.encode({"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, "HS256")
    return {"Authorization": f"Bearer {token}"}


async def _make_contact(session_factory) -> tuple[str, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": phone},
            )
        ).scalar_one()
        await db.commit()
        return str(eid), phone


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"mem-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _seed_fact(
    session_factory,
    contact_id: str,
    *,
    category: str,
    content: str,
    structured: str = "{}",
    active: bool = True,
    source: str = "contact_stated",
) -> None:
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO personal_facts (contact_id, category, content, structured, source, "
                "active) VALUES (CAST(:e AS uuid), :c, :t, CAST(:s AS jsonb), :src, :a)"
            ),
            {
                "e": contact_id,
                "c": category,
                "t": content,
                "s": structured,
                "src": source,
                "a": active,
            },
        )
        await db.commit()


async def _seed_summary(
    session_factory, call_id: str, contact_id: str, *, summary: str, open_plans: str
) -> None:
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO conversation_summaries (call_id, contact_id, summary, open_plans, "
                "model_version) VALUES (CAST(:c AS uuid), CAST(:e AS uuid), :s, CAST(:p AS jsonb), "
                "'test')"
            ),
            {"c": call_id, "e": contact_id, "s": summary, "p": open_plans},
        )
        await db.commit()


def _last_vars(dispatch_spy) -> dict:
    return dispatch_spy.call_args.kwargs["resolved_vars"]


async def test_active_facts_and_latest_summary_carried_outbound(
    client, dispatch_spy, session_factory
):
    contact_id, _ = await _make_contact(session_factory)
    await _seed_fact(session_factory, contact_id, category="person", content="son Tom lives nearby")
    await _seed_fact(session_factory, contact_id, category="routine", content="walks every morning")
    prior = _enqueue_call(client, contact_id)
    await _seed_summary(
        session_factory,
        prior,
        contact_id,
        summary="Chatted about the garden.",
        open_plans='["water the roses", "call the pharmacy"]',
    )

    _enqueue_call(client, contact_id)
    v = _last_vars(dispatch_spy)
    assert "son Tom lives nearby" in v["personal_facts"]
    assert "walks every morning" in v["personal_facts"]
    assert v["last_call_summary"] == "Chatted about the garden."
    assert "water the roses" in v["open_plans"]
    assert "call the pharmacy" in v["open_plans"]


async def test_only_active_facts_carried(client, dispatch_spy, session_factory):
    # Superseded facts (active=false) are history — they must not be re-injected.
    contact_id, _ = await _make_contact(session_factory)
    await _seed_fact(session_factory, contact_id, category="preference", content="likes black tea")
    await _seed_fact(
        session_factory, contact_id, category="preference", content="liked green tea", active=False
    )
    _enqueue_call(client, contact_id)
    v = _last_vars(dispatch_spy)
    assert "likes black tea" in v["personal_facts"]
    assert "green tea" not in v["personal_facts"]


async def test_latest_summary_wins(client, dispatch_spy, session_factory):
    contact_id, _ = await _make_contact(session_factory)
    older = _enqueue_call(client, contact_id)
    await _seed_summary(session_factory, older, contact_id, summary="Older recap.", open_plans="[]")
    newer = _enqueue_call(client, contact_id)
    await _seed_summary(session_factory, newer, contact_id, summary="Newer recap.", open_plans="[]")

    _enqueue_call(client, contact_id)
    assert _last_vars(dispatch_spy)["last_call_summary"] == "Newer recap."


async def test_important_dates_window(client, dispatch_spy, session_factory):
    # important_dates surfaces only dates within ±1 day of today (anniversary match on
    # month/day), so a birthday today is offered while a date months away is not.
    contact_id, _ = await _make_contact(session_factory)
    today = datetime.now(UTC).date()
    far = today + timedelta(days=60)
    await _seed_fact(
        session_factory,
        contact_id,
        category="important_date",
        content="birthday",
        structured=f'{{"date": "{today.isoformat()}", "label": "her birthday"}}',
    )
    await _seed_fact(
        session_factory,
        contact_id,
        category="important_date",
        content="anniversary",
        structured=f'{{"date": "{far.isoformat()}", "label": "wedding anniversary"}}',
    )

    _enqueue_call(client, contact_id)
    v = _last_vars(dispatch_spy)
    assert "her birthday" in v["important_dates"]
    assert "wedding anniversary" not in v["important_dates"]
    # important_date facts are surfaced via important_dates, not duplicated into personal_facts.
    assert "her birthday" not in v["personal_facts"]


async def test_inbound_carries_memory(client, dispatch_spy, session_factory):
    # The inbound resolver in routers/calls.py is a SEPARATE carry site; an contact who
    # calls IN must still be greeted with their remembered facts.
    contact_id, phone = await _make_contact(session_factory)
    await _seed_fact(
        session_factory, contact_id, category="person", content="neighbor Joan helps out"
    )

    resp = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-mem"},
        headers=_worker_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert "neighbor Joan helps out" in resp.json()["resolved_vars"]["personal_facts"]
