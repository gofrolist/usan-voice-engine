"""T043 (US4): post-call summarization writes a summary + extracts facts (Vertex mocked).

``summarization.summarize_call_with`` runs ONE Vertex turn over the call transcript
(``vertexai=True`` + ADC — Constitution II; the transcript PHI never leaves Vertex/
Postgres) and persists a ``conversation_summaries`` row plus any extracted
``personal_facts`` (``source='extracted'``). It is idempotent per call and a no-op
when there is nothing to summarize. The flag gate lives in the fire-and-forget
``summarize_call`` wrapper, so the core is exercised directly here with Vertex mocked.

Written FIRST (Constitution IV) — fails until usan_api.summarization lands.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import summarization
from usan_api.settings import get_settings
from usan_api.vertex_test import VertexTurn

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _make_contact(session_factory) -> str:
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
            )
        ).scalar_one()
        await db.commit()
        return str(eid)


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"sum-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _add_transcript(session_factory, call_id: str, lines: list[tuple[str, str]]) -> None:
    async with session_factory() as db:
        for role, content in lines:
            await db.execute(
                text(
                    "INSERT INTO transcripts (call_id, role, content, started_at) "
                    "VALUES (CAST(:c AS uuid), :r, :t, now())"
                ),
                {"c": call_id, "r": role, "t": content},
            )
        await db.commit()


async def _summaries(session_factory, contact_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT call_id, summary, open_plans, model_version "
                    "FROM conversation_summaries WHERE contact_id = :e ORDER BY id"
                ),
                {"e": contact_id},
            )
        ).all()


async def _facts(session_factory, contact_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT category, content, source FROM personal_facts "
                    "WHERE contact_id = :e ORDER BY id"
                ),
                {"e": contact_id},
            )
        ).all()


def _vertex_returning(payload: dict) -> AsyncMock:
    return AsyncMock(return_value=VertexTurn(text=json.dumps(payload)))


async def test_summarize_writes_summary_and_extracts_facts(
    client, mock_dispatch, session_factory, monkeypatch
):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(
        session_factory,
        call_id,
        [("assistant", "How are you today?"), ("user", "Good. My daughter Maria visits Sundays.")],
    )

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "Ada is well; mentioned her daughter Maria.",
                "open_plans": ["call the doctor tomorrow"],
                "facts": [
                    {"category": "person", "content": "daughter Maria visits on Sundays"},
                    {"category": "routine", "content": "naps after lunch"},
                ],
            }
        ),
    )

    async with session_factory() as db:
        result = await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())
    assert result is not None

    summaries = await _summaries(session_factory, contact_id)
    assert len(summaries) == 1
    assert summaries[0].summary == "Ada is well; mentioned her daughter Maria."
    assert summaries[0].open_plans == ["call the doctor tomorrow"]
    assert summaries[0].model_version  # the summarization model id is recorded for audit

    facts = await _facts(session_factory, contact_id)
    assert {(f.category, f.content) for f in facts} == {
        ("person", "daughter Maria visits on Sundays"),
        ("routine", "naps after lunch"),
    }
    assert all(f.source == "extracted" for f in facts)  # never 'contact_stated' on this path


async def test_summarize_is_idempotent(client, mock_dispatch, session_factory, monkeypatch):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(session_factory, call_id, [("user", "Hello there.")])

    spy = _vertex_returning({"summary": "Brief chat.", "open_plans": [], "facts": []})
    monkeypatch.setattr(summarization, "run_vertex_turn", spy)

    async with session_factory() as db:
        first = await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())
    async with session_factory() as db:
        second = await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    assert first is not None
    assert second is None  # already summarized -> early return, no duplicate row
    assert len(await _summaries(session_factory, contact_id)) == 1
    assert spy.await_count == 1  # the 2nd call must NOT re-invoke (re-bill) Vertex


async def test_summarize_no_transcript_is_noop(client, mock_dispatch, session_factory, monkeypatch):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)  # no transcript added

    spy = _vertex_returning({"summary": "x", "open_plans": [], "facts": []})
    monkeypatch.setattr(summarization, "run_vertex_turn", spy)

    async with session_factory() as db:
        result = await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    assert result is None
    assert await _summaries(session_factory, contact_id) == []
    assert spy.await_count == 0  # nothing to summarize -> Vertex is never called


async def test_summarize_tolerates_malformed_model_json(
    client, mock_dispatch, session_factory, monkeypatch
):
    # A model that ignores the JSON instruction must not crash the pipeline: store the
    # raw text as the summary, extract no facts, and never raise (Observability VI).
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(session_factory, call_id, [("user", "Lovely weather.")])

    monkeypatch.setattr(
        summarization, "run_vertex_turn", AsyncMock(return_value=VertexTurn(text="not json"))
    )

    async with session_factory() as db:
        result = await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    assert result is not None
    summaries = await _summaries(session_factory, contact_id)
    assert len(summaries) == 1
    assert summaries[0].summary == "not json"
    assert await _facts(session_factory, contact_id) == []


async def test_summarize_skips_facts_already_active(
    client, mock_dispatch, session_factory, monkeypatch
):
    # The extracted-fact dedup must compare against the FULL active set (not the 50-row
    # injection cap), so a fact the contact already has is NOT re-inserted every call.
    contact_id = await _make_contact(session_factory)
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO personal_facts (contact_id, category, content, source) VALUES "
                "(CAST(:e AS uuid), 'person', 'daughter Maria visits on Sundays', 'contact_stated')"
            ),
            {"e": contact_id},
        )
        await db.commit()
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(session_factory, call_id, [("user", "Maria came by.")])

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "Maria visited.",
                "open_plans": [],
                "facts": [
                    {"category": "person", "content": "daughter Maria visits on Sundays"},  # dup
                    {"category": "routine", "content": "naps after lunch"},  # new
                ],
            }
        ),
    )
    async with session_factory() as db:
        await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    facts = await _facts(session_factory, contact_id)
    assert [(f.category, f.content) for f in facts] == [
        ("person", "daughter Maria visits on Sundays"),  # the pre-existing row, untouched
        ("routine", "naps after lunch"),  # only the genuinely new fact is added
    ]
    assert [f.source for f in facts] == ["contact_stated", "extracted"]


async def test_summarize_drops_offenum_category_and_caps_facts(
    client, mock_dispatch, session_factory, monkeypatch
):
    # _coerce_facts is the only gate on the extraction path (no Pydantic): a hallucinated
    # category is dropped (it would violate the DB CHECK), content is truncated to 500, and
    # the batch is capped at 20.
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(session_factory, call_id, [("user", "lots to say")])

    facts = [{"category": "bank_pin", "content": "1234"}]  # off-enum -> dropped
    facts.append({"category": "preference", "content": "x" * 900})  # -> truncated to 500
    facts += [{"category": "routine", "content": f"habit {i}"} for i in range(25)]  # cap at 20

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "s", "open_plans": [], "facts": facts}),
    )
    async with session_factory() as db:
        await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    rows = await _facts(session_factory, contact_id)
    assert all(r.category != "bank_pin" for r in rows)  # off-enum never reaches the DB
    assert len(rows) == 20  # _MAX_EXTRACTED_FACTS
    pref = next(r for r in rows if r.category == "preference")
    assert len(pref.content) == 500  # _MAX_FACT_CONTENT_CHARS


async def test_summarize_parses_fenced_json(client, mock_dispatch, session_factory, monkeypatch):
    # Models often wrap JSON in a ```json fence despite the instruction; _strip_code_fence
    # must recover it (else every fenced reply silently degrades to a raw-text recap).
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(session_factory, call_id, [("user", "hi")])

    fenced = (
        '```json\n{"summary": "Fenced recap.", "open_plans": ["see doctor"], '
        '"facts": [{"category": "person", "content": "son Bob"}]}\n```'
    )
    monkeypatch.setattr(
        summarization, "run_vertex_turn", AsyncMock(return_value=VertexTurn(text=fenced))
    )
    async with session_factory() as db:
        await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings())

    summaries = await _summaries(session_factory, contact_id)
    assert summaries[0].summary == "Fenced recap."  # fence stripped + parsed, not stored raw
    assert summaries[0].open_plans == ["see doctor"]
    assert [(f.category, f.content) for f in await _facts(session_factory, contact_id)] == [
        ("person", "son Bob")
    ]


def test_fact_category_enum_matches_summarizer_validator():
    # The summarizer's _VALID_CATEGORIES (which drops hallucinated categories before they
    # hit the personal_facts CHECK) is derived from the schema Literal; pin the closed set
    # so the two cannot drift (the migration 0021 CHECK mirrors the same five values).
    from typing import get_args

    from usan_api.schemas.personalization import FactCategory

    expected = {"person", "routine", "preference", "important_date", "health_context"}
    assert set(get_args(FactCategory)) == expected
    assert expected == summarization._VALID_CATEGORIES


async def test_summarize_call_wrapper_skips_when_disabled(monkeypatch):
    # The fire-and-forget wrapper is flag-gated: with summarization disabled it returns
    # WITHOUT opening a session or calling Vertex (ship-inert, like the other pollers).
    class _Stub:
        summarization_enabled = False
        gcp_project = None

    monkeypatch.setattr(summarization, "get_settings", lambda: _Stub())
    spy = AsyncMock()
    monkeypatch.setattr(summarization, "run_vertex_turn", spy)

    await summarization.summarize_call(uuid.uuid4())  # must not raise, must not call Vertex
    assert spy.await_count == 0


async def test_contact_id_is_nullable(session_factory):
    """Migration 0051 pin: a contact-less (web-call) summary row must be insertable."""
    async with session_factory() as db:
        nullable = (
            await db.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'conversation_summaries' AND column_name = 'contact_id'"
                )
            )
        ).scalar_one()
    assert nullable == "YES"


async def test_upsert_inserts_then_replaces(client, session_factory, mock_dispatch):
    from usan_api.repositories import conversation_summaries as summaries_repo

    contact_id = await _make_contact(session_factory)
    call_id = uuid.UUID(_enqueue_call(client, contact_id))

    async with session_factory() as db:
        first = await summaries_repo.upsert(
            db,
            call_id=call_id,
            contact_id=uuid.UUID(contact_id),
            summary="first",
            open_plans=["plan a"],
            model_version="m1",
        )
        await db.commit()
    assert first.summary == "first"

    async with session_factory() as db:
        second = await summaries_repo.upsert(
            db,
            call_id=call_id,
            contact_id=uuid.UUID(contact_id),
            summary="second",
            open_plans=[],
            model_version="m2",
        )
        await db.commit()
    assert second.summary == "second"
    assert second.open_plans == []
    assert second.model_version == "m2"

    async with session_factory() as db:
        row = await summaries_repo.get_for_call(db, call_id)
    assert row is not None
    assert row.summary == "second"


async def _seeded_call(client, session_factory) -> tuple[str, str]:
    """A queued call with a transcript; returns (call_id, contact_id) as strings."""
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    await _add_transcript(
        session_factory, call_id, [("user", "I feel great"), ("agent", "Wonderful!")]
    )
    return call_id, contact_id


async def test_force_recomputes_and_replaces(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _contact_id = await _seeded_call(client, session_factory)
    settings = get_settings()

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "v1",
                "open_plans": [],
                "facts": [{"category": "preference", "content": "likes tea"}],
            }
        ),
    )
    async with session_factory() as db:
        row = await summarization.summarize_call_with(db, uuid.UUID(call_id), settings)
    assert row is not None
    assert row.summary == "v1"

    # Non-force is idempotent: a second normal run is a no-op.
    async with session_factory() as db:
        assert await summarization.summarize_call_with(db, uuid.UUID(call_id), settings) is None

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning(
            {
                "summary": "v2",
                "open_plans": ["call the doctor"],
                "facts": [{"category": "preference", "content": "likes coffee"}],
            }
        ),
    )
    async with session_factory() as db:
        forced = await summarization.summarize_call_with(
            db, uuid.UUID(call_id), settings, force=True
        )
        await db.commit()  # force is flush-only; the caller commits
    assert forced is not None
    assert forced.summary == "v2"
    assert forced.open_plans == ["call the doctor"]

    # Facts are NOT persisted on force: only the v1 fact exists.
    async with session_factory() as db:
        contents = (await db.execute(text("SELECT content FROM personal_facts"))).scalars().all()
    assert "likes tea" in contents
    assert "likes coffee" not in contents


async def test_force_is_flush_only(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _ = await _seeded_call(client, session_factory)
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "uncommitted", "open_plans": [], "facts": []}),
    )

    async with session_factory() as db:
        await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings(), force=True)
        # NO commit — the row must not be visible from another session.
        async with session_factory() as other:
            from usan_api.repositories import conversation_summaries as summaries_repo

            assert await summaries_repo.get_for_call(other, uuid.UUID(call_id)) is None


async def test_force_contactless_call(client, session_factory, mock_dispatch, monkeypatch):
    call_id, _ = await _seeded_call(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            text("UPDATE calls SET contact_id = NULL WHERE id = CAST(:c AS uuid)"),
            {"c": call_id},
        )
        await db.commit()

    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "web recap", "open_plans": [], "facts": []}),
    )
    async with session_factory() as db:
        row = await summarization.summarize_call_with(
            db, uuid.UUID(call_id), get_settings(), force=True
        )
        await db.commit()
    assert row is not None
    assert row.summary == "web recap"
    assert row.contact_id is None

    # And the non-force path still bails on contact-less calls (unchanged behavior).
    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM conversation_summaries WHERE call_id = CAST(:c AS uuid)"),
            {"c": call_id},
        )
        await db.commit()
    async with session_factory() as db:
        assert (
            await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings()) is None
        )


async def test_force_reenqueues_call_analyzed(client, session_factory, mock_dispatch, monkeypatch):
    """The rerun re-fires the compat call_analyzed webhook event (oracle-faithful)."""
    call_id, _ = await _seeded_call(client, session_factory)
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        _vertex_returning({"summary": "s", "open_plans": [], "facts": []}),
    )
    enqueued = AsyncMock()
    monkeypatch.setattr("usan_api.compat.lifecycle.enqueue_compat_call_event", enqueued)
    async with session_factory() as db:
        await summarization.summarize_call_with(db, uuid.UUID(call_id), get_settings(), force=True)
        await db.commit()
    enqueued.assert_awaited_once()
    assert enqueued.await_args.kwargs["event"] == "call_analyzed"
