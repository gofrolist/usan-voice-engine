import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.conftest import counter_value
from usan_api import livekit_dispatch, quiet_hours
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.observability.custom_metrics import DIAL_REQUEUED_TOTAL
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings


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
    fake.room.delete_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _pin_inside_quiet_hours(monkeypatch) -> datetime:
    """Pin the dial moment to 12:00 UTC (inside [09:00, 21:00) for UTC elders) so
    the dial-time TCPA re-check never re-queues tests aimed at later stages."""
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)
    return now


def _pin_dial_moment(monkeypatch, hour: int, minute: int = 0) -> datetime:
    """Frozen-time variant for the policy scenarios: pin the dial moment to an
    arbitrary UTC wall-clock time (== elder-local for the UTC elders seeded here)."""
    now = datetime(2026, 6, 10, hour, minute, tzinfo=UTC)
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)
    return now


# Sentinel: "no profile at all" — distinct from None, which publishes a profile
# WITHOUT a `policy` section (resolves, statutory by whole-profile precedence).
_NO_PROFILE = object()


async def _publish_policy_profile(db, *, policy=None):
    """Create a profile, optionally set a `policy` section, publish it; returns the id."""
    profile = await profiles_repo.create_profile(
        db, name=f"policy-{uuid.uuid4().hex}", description=None, actor_email="t@usan.test"
    )
    if policy is not None:
        cfg = dict(profile.draft_config)
        cfg["policy"] = policy
        await profiles_repo.update_draft(
            db, profile.id, config=cfg, description=None, actor_email="t@usan.test"
        )
    await profiles_repo.publish(db, profile.id, note=None, actor_email="t@usan.test")
    return profile.id


async def _seed_dialing_retry(
    factory, *, room, tz="UTC", elder_policy=_NO_PROFILE, override_policy=_NO_PROFILE
):
    """A claimed retry row: status=DIALING, attempt 2.

    ``elder_policy``/``override_policy``: ``_NO_PROFILE`` (default) leaves the
    elder/call profile-less; ``None`` publishes a profile with no ``policy``
    section; a dict publishes that policy and assigns it (elder's
    ``agent_profile_id`` / the call's ``profile_override``)."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=tz)
        if elder_policy is not _NO_PROFILE:
            elder.agent_profile_id = await _publish_policy_profile(db, policy=elder_policy)
        call = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            attempt=2,
            livekit_room=room,
        )
        if override_policy is not _NO_PROFILE:
            call.profile_override = await _publish_policy_profile(db, policy=override_policy)
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id, phone


async def _count_children(factory, parent_id):
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_dispatch_and_dial_blocks_on_dnc(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, phone = await _seed_dialing_retry(session_factory, room="usan-outbound-dnc")
    async with session_factory() as db:
        await dnc_repo.add_entry(db, phone, "opt-out")
        await db.commit()

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.DNC_BLOCKED
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0  # DNC is terminal, no retry


@pytest.mark.asyncio
async def test_dispatch_and_dial_misconfig_fails_without_retry(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-mc")
    settings = _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID=None, TELNYX_CALLER_ID=None)
    await livekit_dispatch.dispatch_and_dial(call_id, settings)

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "not_configured"
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_delegates_to_dial_when_ok(monkeypatch, session_factory):
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)
    _pin_inside_quiet_hours(monkeypatch)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-ok2")
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]


@pytest.mark.asyncio
async def test_dispatch_and_dial_marks_elder_missing_failed_not_silent(
    monkeypatch, session_factory
):
    """Elder deleted after claim (ON DELETE SET NULL): the row must go FAILED
    (elder_missing), not stay DIALING for reclaim_stuck_dialing to re-queue
    forever (spec §2.3(2))."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-em")
    async with session_factory() as db:
        await db.execute(update(Call).where(Call.id == call_id).values(elder_id=None))
        await db.commit()

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "elder_missing"
    assert call.ended_at is not None
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    # Elder gone => schedule_retry returns None => the chain settles here.
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_missing_room_marks_failed(monkeypatch, session_factory):
    """Same guard, livekit_room=None: FAILED(elder_missing), never silent."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(session_factory, room=None)

    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "elder_missing"
    assert call.ended_at is not None
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0


@pytest.mark.asyncio
async def test_dispatch_and_dial_requeues_outside_quiet_hours(monkeypatch, session_factory):
    """A clamp is a promise about the past: gate-induced waiting can slide a claim
    past its quiet-hours window. At 23:00 elder-local the dial must NOT proceed —
    the row flips back to QUEUED with a fresh clamp (spec §2.3(1)/§6.3(3))."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=UTC)  # 23:00 EDT on 2026-06-09
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-qh", tz="America/New_York"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.QUEUED
    assert call.scheduled_at == quiet_hours.next_allowed(now, "America/New_York")
    assert call.scheduled_at == datetime(2026, 6, 10, 13, 0, tzinfo=UTC)  # 09:00 EDT
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_for_quiet_hours_refuses_non_dialing_webhook_race(session_factory):
    """The dial-time re-check's re-queue is guarded on DIALING: a row whose
    outcome a racing webhook already wrote (e.g. COMPLETED) between claim and
    re-queue must NOT flip back to QUEUED — the guarded transition claims
    nothing and the terminal outcome is preserved."""
    call_id, _ = await _seed_dialing_retry(session_factory, room="usan-outbound-race")
    async with session_factory() as db:  # the webhook's terminal commit wins the race
        await db.execute(update(Call).where(Call.id == call_id).values(status=CallStatus.COMPLETED))
        await db.commit()

    fresh_clamp = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    async with session_factory() as db:
        requeued = await calls_repo.requeue_for_quiet_hours(db, call_id, scheduled_at=fresh_clamp)
        assert requeued is None  # the guard claimed nothing
        await db.commit()

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.COMPLETED  # webhook outcome preserved
    assert call.scheduled_at is None  # no fresh clamp written


@pytest.mark.asyncio
async def test_dispatch_and_dial_inside_quiet_hours_proceeds(monkeypatch, session_factory):
    """Regression guard: inside [09:00, 21:00) elder-local the dial proceeds."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    now = datetime(2026, 6, 10, 16, 0, tzinfo=UTC)  # 12:00 EDT
    monkeypatch.setattr(livekit_dispatch, "_utcnow", lambda: now)

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-qh-ok", tz="America/New_York"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]


@pytest.mark.asyncio
async def test_dispatch_and_dial_invalid_tz_fails_closed(monkeypatch, session_factory):
    """An invalid elder timezone fails CLOSED: FAILED(invalid_timezone), no dial,
    no retry child (schedule_retry independently refuses invalid-tz — the chain
    settles here)."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)

    call_id, _ = await _seed_dialing_retry(
        session_factory, room="usan-outbound-badtz", tz="Not/AZone"
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.FAILED
    assert call.end_reason == "invalid_timezone"
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
    assert await _count_children(session_factory, call_id) == 0


# --- Wiring site 2: the dial-moment re-check is policy-aware (spec §3.3.2) ---


@pytest.mark.asyncio
async def test_dial_requeued_under_narrowed_policy_window(monkeypatch, session_factory):
    """Elder's profile narrows quiet-hours start to 10:00; at 09:30 local (inside
    statutory [09:00, 21:00) — today's check would dial) the re-check must take
    the requeue_for_quiet_hours path: row back to QUEUED with scheduled_at at the
    POLICY start, the requeue counter incremented, and no dial attempted. This is
    what makes a tightened window effective for already-queued calls (§3.3.2)."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_dial_moment(monkeypatch, 9, 30)  # 09:30 local for a UTC elder

    call_id, _ = await _seed_dialing_retry(
        session_factory,
        room="usan-outbound-pol-rq",
        elder_policy={"quiet_hours_start_local": "10:00"},
    )
    before = counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours")
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.QUEUED
    assert call.scheduled_at == datetime(2026, 6, 10, 10, 0, tzinfo=UTC)  # 10:00 local == UTC
    assert counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours") == before + 1
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_dial_proceeds_inside_policy_window(monkeypatch, session_factory):
    """Regression guard for the narrowed window's INSIDE: at 10:30 local with
    policy start 10:00 the dial proceeds — the policy-aware re-check must not
    over-requeue."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_dial_moment(monkeypatch, 10, 30)  # 10:30 local for a UTC elder

    delegated: list[uuid.UUID] = []

    async def _fake_dial(cid, settings):
        delegated.append(cid)

    monkeypatch.setattr(livekit_dispatch, "dial_and_classify", _fake_dial)

    call_id, _ = await _seed_dialing_retry(
        session_factory,
        room="usan-outbound-pol-ok",
        elder_policy={"quiet_hours_start_local": "10:00"},
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    assert delegated == [call_id]


@pytest.mark.asyncio
async def test_dial_requeue_honors_call_override_policy(monkeypatch, session_factory):
    """Precedence threading pin: the call's profile_override narrows (start
    10:00) while the elder's assigned profile resolves with NO policy section
    (statutory). If only elder_profile_id were threaded the 09:30 dial would
    proceed; the override must win — whole-profile precedence (§3.3.2)."""
    fake = _fake_api()
    monkeypatch.setattr(livekit_dispatch, "build_livekit_api", lambda s: fake)
    monkeypatch.setattr(livekit_dispatch, "get_session_factory", lambda: session_factory)
    _pin_dial_moment(monkeypatch, 9, 30)  # 09:30 local for a UTC elder

    call_id, _ = await _seed_dialing_retry(
        session_factory,
        room="usan-outbound-pol-ovr",
        elder_policy=None,  # published profile WITHOUT a policy section
        override_policy={"quiet_hours_start_local": "10:00"},
    )
    await livekit_dispatch.dispatch_and_dial(call_id, _settings())

    async with session_factory() as db:
        call = await calls_repo.get_call(db, call_id)
    assert call.status is CallStatus.QUEUED
    assert call.scheduled_at == datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    fake.agent_dispatch.create_dispatch.assert_not_awaited()
    fake.sip.create_sip_participant.assert_not_awaited()
