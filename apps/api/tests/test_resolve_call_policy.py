"""resolve_call_policy precedence walk + statutory fallback (Phase A4 spec §3.3.2).

Pins the WHOLE-PROFILE precedence decision: the policy comes from the same
profile resolve_agent_config picks — a resolving profile with ``policy=None``
yields the statutory defaults even when a lower-precedence profile narrows.
"""

import uuid
from datetime import time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallStatus
from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ResolvedPolicy, resolve_call_policy

# The statuses next_retry_delay has ladders for (retry_policy._LADDERS keys).
RETRYABLE_STATUSES = (
    CallStatus.NO_ANSWER,
    CallStatus.VOICEMAIL_LEFT,
    CallStatus.BUSY,
    CallStatus.FAILED,
)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    # These repo tests commit profile rows (defaults persist across tests) and never
    # go through the `client` fixture that truncates. Reset profile state per test so
    # "nothing resolvable" assertions see a clean DB regardless of run order (same
    # discipline as test_agent_config_resolve.py — keep the TRUNCATE sets in sync).
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE agent_profile_versions, agent_profiles RESTART IDENTITY CASCADE")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE agent_profile_versions, agent_profiles RESTART IDENTITY CASCADE")
        )
    await engine.dispose()


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def _published(db, *, policy: dict[str, Any] | None = None) -> uuid.UUID:
    """Create a profile, optionally set a policy section, publish it. Returns the id."""
    profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
    cfg = dict(profile.draft_config)
    if policy is not None:
        cfg["policy"] = policy
    await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
    await repo.publish(db, profile.id, note="v1", actor_email="op")
    return profile.id


def _assert_statutory(resolved: ResolvedPolicy) -> None:
    assert resolved.start_local == time(9)
    assert resolved.end_local == time(21)
    assert resolved.delay_multiplier == 1.0
    for status in RETRYABLE_STATUSES:
        assert resolved.max_attempts_for(status) is None


async def test_statutory_defaults_when_nothing_resolves(session_factory):
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert resolved == ResolvedPolicy(
            start_local=time(9), end_local=time(21), delay_multiplier=1.0
        )
        _assert_statutory(resolved)


async def test_policy_from_override_profile(session_factory):
    async with session_factory() as db:
        pid = await _published(
            db,
            policy={
                "quiet_hours_start_local": "10:30",
                "retry_delay_multiplier": 2.0,
                "retry_max_attempts": {"busy": 0},
            },
        )
        await db.commit()
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=pid, elder_profile_id=None, direction="outbound"
        )
        assert resolved.start_local == time(10, 30)
        assert resolved.end_local == time(21)  # unset side stays statutory
        assert resolved.delay_multiplier == 2.0
        assert resolved.max_attempts_for(CallStatus.BUSY) == 0
        assert resolved.max_attempts_for(CallStatus.NO_ANSWER) is None


async def test_policy_from_elder_profile_when_no_override(session_factory):
    async with session_factory() as db:
        pid = await _published(db, policy={"quiet_hours_start_local": "11:00"})
        await db.commit()
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=None, elder_profile_id=pid, direction="outbound"
        )
        assert resolved.start_local == time(11)
        assert resolved.end_local == time(21)
        assert resolved.delay_multiplier == 1.0


async def test_whole_profile_precedence_override_without_policy_yields_statutory(session_factory):
    # The §3.3.2 pin: precedence is WHOLE-PROFILE, never per-field merge. A live
    # override whose config lacks `policy` loosens back to statutory even though
    # the elder's profile narrows — within the TCPA bound by construction.
    async with session_factory() as db:
        override = await _published(db, policy=None)
        elder = await _published(
            db, policy={"quiet_hours_start_local": "12:00", "retry_delay_multiplier": 3.0}
        )
        await db.commit()
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=override, elder_profile_id=elder, direction="outbound"
        )
        _assert_statutory(resolved)


async def test_profile_with_policy_none_section_yields_statutory(session_factory):
    async with session_factory() as db:
        pid = await _published(db, policy=None)
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        _assert_statutory(resolved)


async def test_invalid_snapshot_falls_through(session_factory):
    # The _resolved_from_profile ValidationError path: a published snapshot that no
    # longer validates falls through the walk; with nothing else resolvable the
    # result is statutory — never an exception, never a half-parsed policy.
    async with session_factory() as db:
        pid = await _published(db, policy={"quiet_hours_start_local": "10:00"})
        await db.commit()
    async with session_factory() as db:
        await db.execute(text("UPDATE agent_profile_versions SET config = '{}'::jsonb"))
        await db.commit()
    async with session_factory() as db:
        resolved = await resolve_call_policy(
            db, profile_override=pid, elder_profile_id=None, direction="outbound"
        )
        _assert_statutory(resolved)
