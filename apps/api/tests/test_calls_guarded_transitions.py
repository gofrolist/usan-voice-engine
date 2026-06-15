"""Genuine two-concurrent-sessions race tests for the guarded call mutators.

Spec §2.1's hardening prerequisite (§10.7): under READ COMMITTED the unlocked
read-modify-write mutators let two overlapping sessions both pass the Python
status check and both commit — a double terminal transition (and, once C4
instruments them, a double ``call.completed`` enqueue no receiver can
collapse). The pre-existing lifecycle race tests are sequential and pass
without row locks; here session A holds its transaction open across a flushed
mutation so session B (a separate engine ⇒ separate connection) must block on
the row lock and re-read A's committed status.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo

Mutator = Callable[[AsyncSession], Awaitable[object]]


@pytest.fixture
async def factories(async_database_url):
    """Two engines ⇒ two distinct connections (genuine row-lock contention)."""
    engine_a = create_async_engine(async_database_url, poolclass=NullPool)
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    yield (
        async_sessionmaker(engine_a, expire_on_commit=False),
        async_sessionmaker(engine_b, expire_on_commit=False),
    )
    await engine_a.dispose()
    await engine_b.dispose()


async def _seed_call(factory, *, status: CallStatus, room: str | None = None) -> uuid.UUID:
    # Unique phone per call: this module shares the long-lived test Postgres with
    # modules that never truncate, so a fixed number would collide on phone_e164.
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


async def _get_call(factory, call_id: uuid.UUID):
    async with factory() as db:
        call = await calls_repo.get_call(db, call_id)
        assert call is not None
        return call


async def _race(factory_a, factory_b, mutator_a: Mutator, mutator_b: Mutator):
    """The §10.7 interleaving: A mutates (flushed, transaction held open), B
    starts on the second connection and must block on the row lock,
    A commits, then B completes.
    """

    async def _run_b() -> object:
        async with factory_b() as db:
            result = await mutator_b(db)
            await db.commit()
            return result

    async with factory_a() as db_a:
        result_a = await mutator_a(db_a)
        task = asyncio.create_task(_run_b())
        await asyncio.sleep(0.2)  # let B reach (and block on) the locked row
        await db_a.commit()
    result_b = await task
    return result_a, result_b


async def test_outcome_voicemail_vs_room_finished_single_winner(factories):
    # POST /v1/calls/{id}/outcome (voicemail) racing the room_finished webhook:
    # exactly one terminal transition may win (spec §2.1 headline race).
    factory_a, factory_b = factories
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = await _seed_call(factory_a, status=CallStatus.IN_PROGRESS, room=room)

    result_a, result_b = await _race(
        factory_a,
        factory_b,
        lambda db: calls_repo.mark_voicemail_left_if_in_progress(db, call_id),
        lambda db: calls_repo.mark_completed_if_in_progress(db, room),
    )

    assert result_a is not None
    assert result_b is None  # the second racer re-reads the terminal status
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.VOICEMAIL_LEFT


async def test_end_call_vs_room_finished_single_winner(factories):
    factory_a, factory_b = factories
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = await _seed_call(factory_a, status=CallStatus.IN_PROGRESS, room=room)

    result_a, result_b = await _race(
        factory_a,
        factory_b,
        lambda db: calls_repo.complete_call_if_in_progress(
            db, call_id, end_reason="check_in_complete"
        ),
        lambda db: calls_repo.mark_completed_if_in_progress(db, room),
    )

    winners = [r for r in (result_a, result_b) if r is not None]
    assert len(winners) == 1
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.COMPLETED
    assert call.end_reason == "check_in_complete"  # the lock holder's write survives


async def test_mark_failed_if_active_vs_dial_failure_single_winner(factories):
    factory_a, factory_b = factories
    call_id = await _seed_call(factory_a, status=CallStatus.DIALING)

    result_a, result_b = await _race(
        factory_a,
        factory_b,
        lambda db: calls_repo.mark_dial_failure(
            db, call_id, CallStatus.NO_ANSWER, end_reason="sip_no_answer"
        ),
        lambda db: calls_repo.mark_failed_if_active(db, call_id, end_reason="internal_error"),
    )

    winners = [r for r in (result_a, result_b) if r is not None]
    assert len(winners) == 1
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.NO_ANSWER  # one terminal outcome — the lock holder's


async def test_requeue_for_quiet_hours_vs_terminal_commit_single_winner(factories):
    # Dial-time quiet-hours requeue racing a terminal dial outcome (§2.1/§10.7
    # interleaving): the requeue must block on the row lock, re-read the
    # committed terminal status, and no-op — resurrecting the row to QUEUED
    # would re-dial a settled call and emit a duplicate call.completed on its
    # next terminal transition.
    factory_a, factory_b = factories
    call_id = await _seed_call(factory_a, status=CallStatus.DIALING)

    result_a, result_b = await _race(
        factory_a,
        factory_b,
        lambda db: calls_repo.mark_dial_failure(
            db, call_id, CallStatus.NO_ANSWER, end_reason="sip_no_answer"
        ),
        lambda db: calls_repo.requeue_for_quiet_hours(
            db, call_id, scheduled_at=datetime(2026, 6, 11, 15, 0, tzinfo=UTC)
        ),
    )

    assert result_a is not None
    assert result_b is None  # the requeue loser re-reads the terminal status
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.NO_ANSWER  # terminal commit wins; no resurrection
    assert call.end_reason == "sip_no_answer"


async def test_mark_answered_after_terminal_is_noop_no_zombie(factories):
    # The pre-existing zombie bug (spec §2.1): a late mark_answered must never
    # resurrect a room_finished-completed call to IN_PROGRESS and pin a slot.
    factory_a, _ = factories
    answered_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    call_id = await _seed_call(factory_a, status=CallStatus.COMPLETED)
    async with factory_a() as db:
        call = await calls_repo.get_call(db, call_id)
        assert call is not None
        call.answered_at = answered_at
        await db.commit()

    async with factory_a() as db:
        result = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_LATE")
        await db.commit()

    assert result is None
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.COMPLETED  # no zombie resurrection
    assert call.answered_at == answered_at  # unchanged
    assert call.sip_call_id is None  # the whole write is gated, not just the status


@pytest.mark.parametrize("start", [CallStatus.DIALING, CallStatus.RINGING])
async def test_mark_answered_from_dialing_still_transitions(factories, start):
    # Pin the happy path (RINGING tolerated too — never assigned today).
    factory_a, _ = factories
    call_id = await _seed_call(factory_a, status=start)

    async with factory_a() as db:
        call = await calls_repo.mark_answered(db, call_id, sip_call_id="SCL_OK")
        await db.commit()

    assert call is not None
    assert call.status is CallStatus.IN_PROGRESS
    assert call.answered_at is not None
    assert call.sip_call_id == "SCL_OK"


async def test_stale_dial_failure_after_reclaim_requeue_is_noop(factories):
    # reclaim_stuck_dialing leaves reclaimed rows QUEUED; the stale dial task's
    # late mark_dial_failure must no-op (executor note 4: DIALING-only guard),
    # or the fresh re-queued attempt is clobbered terminal before it ever dials.
    factory_a, _ = factories
    call_id = await _seed_call(factory_a, status=CallStatus.QUEUED)

    async with factory_a() as db:
        result = await calls_repo.mark_dial_failure(
            db, call_id, CallStatus.NO_ANSWER, end_reason="sip_no_answer"
        )
        await db.commit()

    assert result is None
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.QUEUED
    assert call.ended_at is None
    assert call.end_reason is None


async def test_dial_failure_on_terminal_is_noop(factories):
    factory_a, _ = factories
    call_id = await _seed_call(factory_a, status=CallStatus.COMPLETED)

    async with factory_a() as db:
        result = await calls_repo.mark_dial_failure(
            db, call_id, CallStatus.FAILED, end_reason="dial_error", error={"reason": "late"}
        )
        await db.commit()

    assert result is None
    call = await _get_call(factory_a, call_id)
    assert call.status is CallStatus.COMPLETED  # row untouched
    assert call.ended_at is None
    assert call.end_reason is None
    assert call.error is None
