"""call.started / call.completed enqueue inside the guarded call mutators (Task C4).

Pins spec §2.1's integration shape (a): every enqueue-bearing mutator in
``repositories/calls.py`` fans the event into the transactional outbox INSIDE
its own guarded transition, after the flush — so the business change and the
event commit or roll back together, the guard's no-op paths enqueue nothing
(the §10.7 "zero enqueues" half), and the C3 race produces exactly ONE
``call.completed`` occurrence (the spec's headline double-emit fix).
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, WebhookDelivery
from usan_api.db.models import WebhookEndpoint as _Endpoint
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.settings import Settings


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Wraps the test (before AND after): leftover webhook_endpoints rows would
    # change fan-out counts here and in unrelated modules (test_webhook_outbox
    # precedent); this module never uses the `client` fixture's TRUNCATE.
    async def _wipe():
        async with session_factory() as db:
            await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
            await db.commit()

    await _wipe()
    yield
    await _wipe()


@pytest.fixture
async def endpoint(session_factory) -> uuid.UUID:
    """One enabled endpoint subscribed to BOTH call lifecycle events."""
    async with session_factory() as db:
        ep = _Endpoint(
            url="https://hooks.example.com/sink",
            secret="a" * 64,
            events=["call.started", "call.completed"],
        )
        db.add(ep)
        await db.commit()
        return ep.id


async def _seed_call(factory, *, status: CallStatus, room: str | None = None) -> uuid.UUID:
    # Unique phone per call: this module shares the long-lived test Postgres
    # with modules that never truncate contacts/calls.
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=room,
        )
        await db.commit()
        return call.id


async def _deliveries(db, event: str) -> list[WebhookDelivery]:
    """Pending outbox rows for one event type."""
    result = await db.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.event == event, WebhookDelivery.status == "pending"
        )
    )
    return list(result.scalars().all())


async def _total_deliveries(db) -> int:
    result = await db.execute(select(func.count()).select_from(WebhookDelivery))
    return int(result.scalar_one())


@pytest.mark.parametrize(
    ("seed_status", "expected_status", "mutate"),
    [
        pytest.param(
            CallStatus.IN_PROGRESS,
            "completed",
            lambda db, call_id, room: calls_repo.mark_completed_if_in_progress(db, room),
            id="mark_completed_if_in_progress",
        ),
        pytest.param(
            CallStatus.IN_PROGRESS,
            "voicemail_left",
            lambda db, call_id, room: calls_repo.mark_voicemail_left_if_in_progress(db, call_id),
            id="mark_voicemail_left_if_in_progress",
        ),
        pytest.param(
            CallStatus.IN_PROGRESS,
            "completed",
            lambda db, call_id, room: calls_repo.complete_call_if_in_progress(
                db, call_id, end_reason="check_in_complete"
            ),
            id="complete_call_if_in_progress",
        ),
        pytest.param(
            CallStatus.DIALING,
            "failed",
            lambda db, call_id, room: calls_repo.mark_failed_if_active(
                db, call_id, end_reason="internal_error"
            ),
            id="mark_failed_if_active",
        ),
        pytest.param(
            CallStatus.DIALING,
            "no_answer",
            lambda db, call_id, room: calls_repo.mark_dial_failure(
                db, call_id, CallStatus.NO_ANSWER, end_reason="sip_no_answer"
            ),
            id="mark_dial_failure",
        ),
    ],
)
async def test_each_terminal_mutator_enqueues_one_completed(
    session_factory, endpoint, seed_status, expected_status, mutate
):
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = await _seed_call(session_factory, status=seed_status, room=room)

    async with session_factory() as db:
        result = await mutate(db, call_id, room)
        await db.commit()
    assert result is not None

    async with session_factory() as db:
        rows = await _deliveries(db, "call.completed")
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert data["status"] == expected_status
    assert data["call_id"] == str(call_id)


@pytest.mark.parametrize(
    ("seed_status", "mutate"),
    [
        pytest.param(
            CallStatus.QUEUED,
            lambda db, call_id: calls_repo.mark_dial_failure(
                db, call_id, CallStatus.NO_ANSWER, end_reason="sip_no_answer"
            ),
            id="stale-dial-failure-on-requeued",
        ),
        pytest.param(
            CallStatus.COMPLETED,
            lambda db, call_id: calls_repo.mark_dial_failure(
                db, call_id, CallStatus.FAILED, end_reason="dial_error"
            ),
            id="dial-failure-on-terminal",
        ),
        pytest.param(
            CallStatus.COMPLETED,
            lambda db, call_id: calls_repo.mark_answered(db, call_id, sip_call_id="SCL_LATE"),
            id="answered-after-terminal",
        ),
    ],
)
async def test_noop_mutators_enqueue_nothing(session_factory, endpoint, seed_status, mutate):
    # The §10.7 "zero enqueues" half the guard tests alone cannot prove: a
    # SUBSCRIBED endpoint exists, yet the no-op path inserts nothing at all.
    call_id = await _seed_call(session_factory, status=seed_status)

    async with session_factory() as db:
        result = await mutate(db, call_id)
        await db.commit()
    assert result is None

    async with session_factory() as db:
        assert await _total_deliveries(db) == 0


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_x",
        "TELNYX_CALLER_ID": "+15551230000",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


def _fake_api() -> MagicMock:
    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.sip.list_outbound_trunk = AsyncMock(return_value=SimpleNamespace(items=[]))
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


def _twirp_busy() -> Exception:
    exc = Exception("SIP 486 Busy Here")
    exc.metadata = {"sip_status_code": "486"}
    return exc


async def test_stale_dial_task_caller_path_no_child_no_event(
    monkeypatch, session_factory, endpoint
):
    # Composite caller path (§10.7's reclaim race, end to end): the row was
    # re-queued by reclaim_stuck_dialing while a stale dial task was still in
    # flight. Its late failure must (a) no-op the guarded mark_dial_failure,
    # (b) produce NO spurious retry child from the trailing unconditional
    # schedule_retry (QUEUED is not a retryable terminal status), and (c)
    # enqueue zero webhook deliveries despite the subscribed endpoint.
    from usan_api import livekit_dispatch

    fake = _fake_api()
    fake.sip.create_sip_participant.side_effect = _twirp_busy()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = await _seed_call(session_factory, status=CallStatus.QUEUED, room=room)

    await livekit_dispatch._dial_and_classify(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
        assert call is not None
        assert call.status is CallStatus.QUEUED  # the fresh attempt survives
        children = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == call_id)
        )
        assert int(children.scalar_one()) == 0
        assert await _total_deliveries(db) == 0


async def test_set_status_enqueues_only_nonterminal_to_terminal(session_factory, endpoint):
    call_id = await _seed_call(session_factory, status=CallStatus.QUEUED)

    async with session_factory() as db:
        await calls_repo.set_status(db, call_id, CallStatus.DIALING)
        await db.commit()
    async with session_factory() as db:
        assert await _deliveries(db, "call.completed") == []  # non-terminal -> non-terminal

    async with session_factory() as db:
        await calls_repo.set_status(db, call_id, CallStatus.FAILED)
        await db.commit()
    async with session_factory() as db:
        rows = await _deliveries(db, "call.completed")
        assert len(rows) == 1  # DIALING -> FAILED crosses into terminal
        assert rows[0].payload["data"]["status"] == "failed"

    # Terminal -> terminal enqueues nothing new (§10.7).
    completed_id = await _seed_call(session_factory, status=CallStatus.COMPLETED)
    async with session_factory() as db:
        await calls_repo.set_status(db, completed_id, CallStatus.FAILED)
        await db.commit()
    async with session_factory() as db:
        assert len(await _deliveries(db, "call.completed")) == 1  # unchanged


async def test_dnc_at_birth_enqueues(session_factory, endpoint):
    # Terminal at birth: DNC_BLOCKED rows never pass through a terminal
    # mutator, so the creators must emit (spec §2.1 table).
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    schedule_id = uuid.uuid4()
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(db, name="A", phone_e164=phone, timezone="UTC")
        adhoc = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
        )
        root = await calls_repo.create_materialized_root(
            db,
            contact_id=contact.id,
            status=CallStatus.DNC_BLOCKED,
            idempotency_key=f"sched:{schedule_id}:2026-06-10",
            scheduled_at=None,
            dynamic_vars={},
            profile_override=None,
        )
        await db.commit()
        adhoc_id, root_id = adhoc.id, root.id

    async with session_factory() as db:
        rows = await _deliveries(db, "call.completed")
    assert len(rows) == 2
    assert all(r.payload["data"]["status"] == "dnc_blocked" for r in rows)
    assert {r.payload["data"]["call_id"] for r in rows} == {str(adhoc_id), str(root_id)}


async def test_cancel_queued_tips_one_event_per_returned_row(session_factory, endpoint):
    q1 = await _seed_call(session_factory, status=CallStatus.QUEUED)
    q2 = await _seed_call(session_factory, status=CallStatus.QUEUED)
    in_progress = await _seed_call(session_factory, status=CallStatus.IN_PROGRESS)

    async with session_factory() as db:
        flipped = await calls_repo.cancel_queued_tips(db, [q1, q2, in_progress])
        await db.commit()
    assert flipped == 2  # -> int signature kept (executor note 4); count unchanged

    async with session_factory() as db:
        rows = await _deliveries(db, "call.completed")
    assert len(rows) == 2
    assert all(r.payload["data"]["status"] == "cancelled" for r in rows)
    assert {r.payload["data"]["call_id"] for r in rows} == {str(q1), str(q2)}


async def test_mark_answered_enqueues_started_once(session_factory, endpoint):
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING)

    async with session_factory() as db:
        first = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_1")
        await db.commit()
    assert first is not None

    async with session_factory() as db:
        second = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_2")
        await db.commit()
    assert second is None  # now IN_PROGRESS: the guard no-ops

    async with session_factory() as db:
        rows = await _deliveries(db, "call.started")
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert data["call_id"] == str(call_id)
    assert data["answered_at"] is not None


async def test_create_inbound_call_enqueues_started(session_factory, endpoint):
    # Inbound calls are answered at birth (spec §2.1 table); contact_id may be
    # NULL for an unknown caller and origin is always null on inbound.
    async with session_factory() as db:
        call = await calls_repo.create_inbound_call(
            db, contact_id=None, livekit_room=f"usan-inbound-{uuid.uuid4()}"
        )
        await db.commit()
        call_id = call.id

    async with session_factory() as db:
        rows = await _deliveries(db, "call.started")
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert data["call_id"] == str(call_id)
    assert data["direction"] == "inbound"
    assert data["origin"] is None
    assert data["contact_id"] is None


async def test_race_emits_exactly_one_completed(session_factory, endpoint, async_database_url):
    # The spec's headline fix (§2.1/§10.7): re-run C3's voicemail-vs-
    # room_finished interleaving WITH a subscribed endpoint — the losing racer
    # re-reads the terminal status, returns None, and enqueues nothing, so
    # exactly one call.completed occurrence exists after both commits.
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    try:
        room = f"usan-outbound-{uuid.uuid4()}"
        call_id = await _seed_call(session_factory, status=CallStatus.IN_PROGRESS, room=room)

        async def _run_b():
            async with factory_b() as db:
                result = await calls_repo.mark_completed_if_in_progress(db, room)
                await db.commit()
                return result

        async with session_factory() as db_a:
            result_a = await calls_repo.mark_voicemail_left_if_in_progress(db_a, call_id)
            task = asyncio.create_task(_run_b())
            await asyncio.sleep(0.2)  # let B reach (and block on) the locked row
            await db_a.commit()
        result_b = await task

        assert result_a is not None
        assert result_b is None
        async with session_factory() as db:
            rows = await _deliveries(db, "call.completed")
        assert len(rows) == 1
        assert rows[0].payload["data"]["status"] == "voicemail_left"
    finally:
        await engine_b.dispose()


async def test_rollback_discards_transition_and_event_together(session_factory, endpoint):
    # Atomicity (spec §2.1): crash-between ⇒ NEITHER the business change nor
    # the event survives.
    call_id = await _seed_call(session_factory, status=CallStatus.IN_PROGRESS)

    async with session_factory() as db:
        result = await calls_repo.complete_call_if_in_progress(
            db, call_id, end_reason="check_in_complete"
        )
        assert result is not None  # transition + enqueue both flushed
        await db.rollback()

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
        assert call is not None
        assert call.status is CallStatus.IN_PROGRESS
        assert await _total_deliveries(db) == 0


async def test_zero_endpoints_zero_rows_zero_errors(session_factory):
    # The ship-inert posture (spec §2.1): with no registered endpoints the
    # mutators behave exactly as before — zero rows, zero errors.
    call_id = await _seed_call(session_factory, status=CallStatus.DIALING)

    async with session_factory() as db:
        started = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_OK")
        await db.commit()
    assert started is not None
    assert started.status is CallStatus.IN_PROGRESS

    async with session_factory() as db:
        completed = await calls_repo.complete_call_if_in_progress(
            db, call_id, end_reason="check_in_complete"
        )
        await db.commit()
    assert completed is not None
    assert completed.status is CallStatus.COMPLETED

    async with session_factory() as db:
        assert await _total_deliveries(db) == 0
