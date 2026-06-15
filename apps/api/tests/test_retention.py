import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import retention
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import (
    Call,
    CallBatchTarget,
    CallSchedule,
    ConversationSummary,
    PersonalFact,
    Transcript,
)
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import conversation_summaries as summaries_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.schemas.batch import BatchTargetIn


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory, *, status: CallStatus, dynamic_vars: dict) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
            dynamic_vars=dynamic_vars,
        )
        await db.commit()
        return call.id


async def _add_transcript(factory, call_id: uuid.UUID) -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    async with factory() as db:
        await transcripts_repo.create_transcript_segments(
            db,
            call_id=call_id,
            segments=[{"role": "user", "content": "PHI here", "started_at": now}],
        )
        await db.commit()


# A clock far in the future makes every freshly-created row older than the window.
_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_purge_deletes_old_transcripts(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.COMPLETED, dynamic_vars={})
    await _add_transcript(session_factory, call_id)
    async with session_factory() as db:
        transcripts, _, _ = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert transcripts == 1
    async with session_factory() as db:
        remaining = await db.execute(
            select(func.count()).select_from(Transcript).where(Transcript.call_id == call_id)
        )
    assert remaining.scalar_one() == 0


@pytest.mark.asyncio
async def test_purge_scrubs_dynamic_vars_on_terminal_calls(session_factory):
    call_id = await _seed_call(
        session_factory, status=CallStatus.COMPLETED, dynamic_vars={"elder_name": "Ada"}
    )
    async with session_factory() as db:
        _, scrubbed, _ = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 1
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.dynamic_vars == {}


@pytest.mark.asyncio
async def test_purge_nulls_recording_uri_on_terminal_calls(session_factory):
    # A terminal call whose only remaining PHI is the recording URI must still be
    # scrubbed, so GET /calls/{id} can no longer mint a signed URL for the audio.
    call_id = await _seed_call(session_factory, status=CallStatus.COMPLETED, dynamic_vars={})
    async with session_factory() as db:
        row = await db.get(Call, call_id)
        row.recording_uri = "gs://bkt/recordings/2099-01-01/x.ogg"
        await db.commit()
    async with session_factory() as db:
        _, scrubbed, _ = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 1
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.recording_uri is None


@pytest.mark.asyncio
async def test_purge_leaves_non_terminal_calls_untouched(session_factory):
    call_id = await _seed_call(
        session_factory, status=CallStatus.IN_PROGRESS, dynamic_vars={"elder_name": "Ada"}
    )
    async with session_factory() as db:
        _, scrubbed, _ = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 0
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.dynamic_vars == {"elder_name": "Ada"}


@pytest.mark.asyncio
async def test_purge_keeps_recent_rows(session_factory):
    # With a present-day clock, just-created rows are inside the window and survive.
    call_id = await _seed_call(
        session_factory, status=CallStatus.COMPLETED, dynamic_vars={"elder_name": "Ada"}
    )
    await _add_transcript(session_factory, call_id)
    recent = datetime.now(UTC) + timedelta(seconds=5)
    async with session_factory() as db:
        transcripts, scrubbed, _ = await retention.purge_expired(db, days=30, now=recent)
        await db.commit()
    assert transcripts == 0
    assert scrubbed == 0


async def _seed_memory_phi(factory) -> uuid.UUID:
    """An elder with a conversation summary + an extracted personal fact (both PHI)."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db, elder_id=elder.id, direction=CallDirection.OUTBOUND, status=CallStatus.COMPLETED
        )
        await summaries_repo.create(
            db,
            call_id=call.id,
            elder_id=elder.id,
            summary="recap with PHI",
            open_plans=["see the doctor Tuesday"],
            model_version="m",
        )
        await personal_facts_repo.create(
            db,
            elder_id=elder.id,
            category="person",
            content="son named Bob",
            structured=None,
            source="extracted",
        )
        await db.commit()
        return elder.id


@pytest.mark.asyncio
async def test_purge_memory_phi_deletes_old_records(session_factory):
    # M5: elder-memory PHI (recaps, facts, survey scores, reports) past the window is DELETED.
    eid = await _seed_memory_phi(session_factory)
    async with session_factory() as db:
        counts = await retention.purge_memory_phi(db, days=30, now=_FUTURE)
        await db.commit()
    assert counts["conversation_summaries"] == 1
    assert counts["personal_facts"] == 1
    async with session_factory() as db:
        n_sum = await db.execute(
            select(func.count())
            .select_from(ConversationSummary)
            .where(ConversationSummary.elder_id == eid)
        )
        n_fact = await db.execute(
            select(func.count()).select_from(PersonalFact).where(PersonalFact.elder_id == eid)
        )
    assert n_sum.scalar_one() == 0
    assert n_fact.scalar_one() == 0


@pytest.mark.asyncio
async def test_purge_memory_phi_keeps_recent_records(session_factory):
    # With a present-day clock, just-created memory PHI is inside the window and survives.
    eid = await _seed_memory_phi(session_factory)
    recent = datetime.now(UTC) + timedelta(seconds=5)
    async with session_factory() as db:
        counts = await retention.purge_memory_phi(db, days=30, now=recent)
        await db.commit()
    assert counts["conversation_summaries"] == 0
    assert counts["personal_facts"] == 0
    async with session_factory() as db:
        n_fact = await db.execute(
            select(func.count()).select_from(PersonalFact).where(PersonalFact.elder_id == eid)
        )
    assert n_fact.scalar_one() == 1


async def _seed_elder(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        await db.commit()
        return elder.id


async def _seed_batch_targets(factory, *, n_targets: int, dynamic_vars: dict) -> uuid.UUID:
    elder_id = await _seed_elder(factory)
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="retention batch",
            idempotency_key=None,
            payload_digest="e" * 64,
            trigger_at=None,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=None,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=elder_id, dynamic_vars=dynamic_vars)] * n_targets,
        )
        await db.commit()
        return batch.id


async def _list_targets(factory, batch_id: uuid.UUID) -> list[CallBatchTarget]:
    async with factory() as db:
        result = await db.execute(
            select(CallBatchTarget)
            .where(CallBatchTarget.batch_id == batch_id)
            .order_by(CallBatchTarget.target_index)
        )
        return list(result.scalars())


@pytest.mark.asyncio
async def test_purge_scrubs_settled_batch_target_vars(session_factory):
    # This module has no autouse truncate; clear batch rows left behind by other
    # modules so the scrubbed-rowcount assertion below is deterministic.
    async with session_factory() as db:
        await db.execute(text("TRUNCATE call_batch_targets, call_batches CASCADE"))
        await db.commit()

    vars_ = {"first_name": "Rose"}
    batch_id = await _seed_batch_targets(session_factory, n_targets=6, dynamic_vars=vars_)
    # (status, finalized_at): settled+old -> scrubbed; unsettled or fresh -> kept.
    plan = [
        ("done", None),
        ("skipped", None),
        ("cancelled", None),
        ("pending", None),
        ("materialized", None),
        ("done", _FUTURE),  # settled but finalized inside the window
    ]
    targets = await _list_targets(session_factory, batch_id)
    async with session_factory() as db:
        for target, (status, finalized_at) in zip(targets, plan, strict=True):
            await db.execute(
                update(CallBatchTarget)
                .where(CallBatchTarget.id == target.id)
                .values(status=status, finalized_at=finalized_at)
            )
        await db.commit()

    # The target scrub rides the SAME purge_expired transaction (third statement,
    # third return slot) — a crash can't scrub calls but leave the source copy.
    async with session_factory() as db:
        transcripts, calls, batch_targets = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert batch_targets == 3

    targets = await _list_targets(session_factory, batch_id)
    assert [t.dynamic_vars for t in targets] == [{}, {}, {}, vars_, vars_, vars_]

    # Idempotent: a second pass finds no remaining PHI-bearing settled targets.
    async with session_factory() as db:
        _, _, again = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert again == 0


@pytest.mark.asyncio
async def test_purge_never_touches_schedule_vars(session_factory):
    # call_schedules.dynamic_vars are live re-used config, deliberately exempt
    # from retention (spec §8) — the operator PHI-removal path is PATCH/DELETE.
    elder_id = await _seed_elder(session_factory)
    async with session_factory() as db:
        schedule = await schedules_repo.create_schedule(
            db,
            elder_id=elder_id,
            window_start_local=time(9, 0),
            window_end_local=time(11, 0),
            days_of_week=127,
            enabled=True,
            dynamic_vars={"first_name": "Ada"},
            profile_override=None,
            next_run_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
        )
        await db.commit()
        schedule_id = schedule.id

    # _FUTURE puts the schedule far past any cutoff — it must still survive.
    async with session_factory() as db:
        await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()

    async with session_factory() as db:
        row = await db.get(CallSchedule, schedule_id)
    assert row is not None
    assert row.dynamic_vars == {"first_name": "Ada"}


@pytest.mark.asyncio
async def test_run_poller_noop_when_retention_unset():
    import asyncio

    from usan_api.settings import Settings

    settings = Settings(
        DATABASE_URL="postgresql://u:p@host/db",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        JWT_SIGNING_KEY="s" * 32,
        OPERATOR_API_KEY="o" * 32,
    )
    assert settings.phi_retention_days is None
    stop = asyncio.Event()
    stop.set()
    # Returns immediately without starting a loop because retention is unset.
    await retention.run_poller(settings, stop)
