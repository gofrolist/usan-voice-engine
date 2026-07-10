"""T057 (US6): contract tests for the wellbeing tools.

``record_survey`` (POST /v1/tools/record_survey) records the monthly wellbeing survey
exactly once per contact per calendar month — a repeat within the month is a no-op that
returns the existing row (unique ``(contact_id, period_month)``; FR-032 / SC-008).

``get_activity`` (POST /v1/tools/get_activity) returns a mood-boosting activity not used
recently and records the use, so consecutive calls do not repeat until the catalog is
exhausted (FR-034 / SC-009). The least-recently-used selection itself is unit-tested
against ``activities_catalog.select_activity`` (pure) for the 30-day / last-3 / exhaustion
edges; the endpoint test pins the end-to-end non-repeat.

Written FIRST (Constitution IV) — fails until the catalog + schema + repos + endpoints land.
"""

import uuid
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token
from usan_api import livekit_dispatch

# Minimal duck-typed history row for the pure selection tests (most-recent-first).
Use = namedtuple("Use", ["activity_key", "used_at"])


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


async def _make_contact(session_factory, *, tz: str = "UTC") -> str:
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, :tz) RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}", "tz": tz},
            )
        ).scalar_one()
        await db.commit()
        return str(eid)


async def _seed_calls(session_factory, contact_id: str, n: int) -> list[str]:
    """Direct-DB call rows (token scopes only) — no enqueue POST/dispatch needed."""
    ids = [str(uuid.uuid4()) for _ in range(n)]
    async with session_factory() as db:
        for cid in ids:
            await db.execute(
                text(
                    "INSERT INTO calls (id, contact_id, direction, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:e AS uuid), 'outbound', 'completed')"
                ),
                {"id": cid, "e": contact_id},
            )
        await db.commit()
    return ids


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"wb-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


# --- record_survey ---------------------------------------------------------


async def _surveys(session_factory, contact_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT id, period_month, loneliness, mood, satisfaction, raw "
                    "FROM wellbeing_survey_results WHERE contact_id = :e ORDER BY id"
                ),
                {"e": contact_id},
            )
        ).all()


def _record_survey(client, call_id: str, **body):
    return client.post(
        "/v1/tools/record_survey",
        json={"call_id": call_id, **body},
        headers=_auth(call_id),
    )


async def test_record_survey_inserts_for_current_month(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)

    r = _record_survey(client, call_id, loneliness=2, mood=4, satisfaction=3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["id"], int)
    # period_month is a first-of-month anchor (data-model).
    assert body["period_month"].endswith("-01")

    rows = await _surveys(session_factory, contact_id)
    assert len(rows) == 1
    assert (rows[0].loneliness, rows[0].mood, rows[0].satisfaction) == (2, 4, 3)


async def test_record_survey_is_once_per_month_idempotent(client, mock_dispatch, session_factory):
    # Unique (contact_id, period_month): a second survey the same month returns the existing
    # row and writes nothing new (FR-032 / SC-008) — no duplicate, no 409.
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)

    first = _record_survey(client, call_id, loneliness=1, mood=2, satisfaction=2)
    assert first.status_code == 200, first.text
    second = _record_survey(client, call_id, loneliness=5, mood=5, satisfaction=5)
    assert second.status_code == 200, second.text

    assert second.json()["id"] == first.json()["id"]  # returns the existing row
    rows = await _surveys(session_factory, contact_id)
    assert len(rows) == 1  # the repeat wrote nothing
    assert (rows[0].loneliness, rows[0].mood, rows[0].satisfaction) == (1, 2, 2)


async def test_record_survey_rejects_out_of_scale(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    r = _record_survey(client, call_id, mood=9)  # 1-5 scale
    assert r.status_code == 422, r.text
    assert await _surveys(session_factory, contact_id) == []


async def test_record_survey_token_scoped_to_other_call_is_rejected(
    client, mock_dispatch, session_factory
):
    contact_id = await _make_contact(session_factory)
    call_a = _enqueue_call(client, contact_id)
    call_b = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/record_survey",
        json={"call_id": call_b, "mood": 3},
        headers=_auth(call_a),
    )
    assert r.status_code == 403, r.text
    assert await _surveys(session_factory, contact_id) == []


# --- get_activity ----------------------------------------------------------


def _get_activity(client, call_id: str, **body):
    return client.post(
        "/v1/tools/get_activity",
        json={"call_id": call_id, **body},
        headers=_auth(call_id),
    )


async def test_get_activity_returns_script_and_records_use(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)

    r = _get_activity(client, call_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activity_key"]
    assert body["title"]
    assert body["script"]

    async with session_factory() as db:
        rows = (
            await db.execute(
                text("SELECT activity_key, call_id FROM activity_history WHERE contact_id = :e"),
                {"e": contact_id},
            )
        ).all()
    assert len(rows) == 1  # the use was recorded
    assert rows[0].activity_key == body["activity_key"]
    assert str(rows[0].call_id) == call_id


async def test_get_activity_does_not_repeat_until_exhausted(client, session_factory):
    # Consecutive calls return distinct activities until the catalog is exhausted, then
    # fall back to the least-recently-used (the first one used) — never two in a row repeat
    # while a fresh one exists (FR-034 / SC-009). The LRU policy lives in the get_activity
    # handler; the call rows are only token scopes, so they are seeded directly instead of
    # one enqueue POST per activity.
    from usan_api import activities_catalog

    total = len(activities_catalog.list_activities("any"))
    contact_id = await _make_contact(session_factory)
    call_ids = await _seed_calls(session_factory, contact_id, total + 1)

    seen: list[str] = []
    for call_id in call_ids[:total]:
        key = _get_activity(client, call_id).json()["activity_key"]
        seen.append(key)
    assert len(set(seen)) == total  # every distinct activity used, no repeat yet

    # Catalog now exhausted -> the next pick is the least-recently-used (the first used).
    assert _get_activity(client, call_ids[total]).json()["activity_key"] == seen[0]


async def test_get_activity_kind_filter(client, mock_dispatch, session_factory):
    from usan_api import activities_catalog

    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    key = _get_activity(client, call_id, kind="breathing").json()["activity_key"]
    assert activities_catalog.by_key(key).kind == "breathing"


async def test_get_activity_rejects_unknown_kind(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    r = _get_activity(client, call_id, kind="dancing")  # closed set
    assert r.status_code == 422, r.text


# --- select_activity (pure LRU policy, FR-034) ------------------------------


def test_select_activity_prefers_never_used():
    from usan_api import activities_catalog

    now = datetime(2026, 6, 14, tzinfo=UTC)
    catalog = activities_catalog.list_activities("any")
    first = catalog[0]
    history = [Use(first.key, now - timedelta(days=1))]
    chosen = activities_catalog.select_activity("any", history, now=now)
    assert chosen.key != first.key  # a never-used one wins over the recently-used first


def test_select_activity_excludes_within_30_days_union_last_3():
    from usan_api import activities_catalog

    now = datetime(2026, 6, 14, tzinfo=UTC)
    keys = [a.key for a in activities_catalog.list_activities("any")]
    # Every key used within the last 30 days -> all excluded -> fall back to the
    # least-recently-used overall (the oldest most-recent use).
    history = [Use(k, now - timedelta(days=i + 1)) for i, k in enumerate(keys)]
    chosen = activities_catalog.select_activity("any", history, now=now)
    assert chosen.key == keys[-1]  # used longest ago == least-recently-used


def test_select_activity_30_day_expiry_reenables_old_activity():
    from usan_api import activities_catalog

    now = datetime(2026, 6, 14, tzinfo=UTC)
    keys = [a.key for a in activities_catalog.list_activities("any")]
    old = keys[0]
    # Every OTHER activity was used within the last 30 days (so all are excluded), while
    # `old` was last used 40 days ago — outside the 30-day window AND not among the last 3
    # uses. So `old` is the only eligible one and is reselected, proving the recency window
    # re-enables a long-unused activity rather than excluding it forever (FR-034).
    recent = [Use(k, now - timedelta(days=i + 1)) for i, k in enumerate(keys[1:])]
    history = [*recent, Use(old, now - timedelta(days=40))]
    chosen = activities_catalog.select_activity("any", history, now=now)
    assert chosen.key == old
