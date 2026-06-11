"""Feature A (spec §3.1): `profile_override` on `POST /v1/calls`.

B1: request/response schema + `create_call` kwarg threading. The column itself
exists since migration 0010 — only the ad-hoc write path is new here.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def _clean(session_factory):
    # Not autouse: the pure schema tests in this module must not pay for Postgres.
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls, agent_profiles, elders RESTART IDENTITY CASCADE"))
        await db.commit()


def test_create_call_request_profile_override_optional_uuid() -> None:
    elder_id = uuid.uuid4()
    omitted = CreateCallRequest(elder_id=elder_id, idempotency_key="k")
    assert omitted.profile_override is None

    pid = uuid.uuid4()
    given = CreateCallRequest(elder_id=elder_id, idempotency_key="k", profile_override=str(pid))
    assert given.profile_override == pid
    assert isinstance(given.profile_override, uuid.UUID)

    with pytest.raises(ValidationError):
        CreateCallRequest(elder_id=elder_id, idempotency_key="k", profile_override="not-a-uuid")


async def _seed_elder_and_profile(factory) -> tuple[uuid.UUID, uuid.UUID]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Override Elder", phone_e164=phone, timezone="America/New_York"
        )
        profile = await profiles_repo.create_profile(
            db, name="override-profile", description=None, actor_email="admin@example.com"
        )
        await db.commit()
        return elder.id, profile.id


@pytest.mark.usefixtures("_clean")
async def test_create_call_persists_profile_override(session_factory) -> None:
    elder_id, pid = await _seed_elder_and_profile(session_factory)

    async with session_factory() as db:
        with_override = await calls_repo.create_call(
            db,
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            profile_override=pid,
        )
        without_override = await calls_repo.create_call(
            db,
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
        )
        await db.commit()
        with_id, without_id = with_override.id, without_override.id

    async with session_factory() as db:
        persisted = await db.get(Call, with_id)
        assert persisted is not None
        assert persisted.profile_override == pid
        bare = await db.get(Call, without_id)
        assert bare is not None
        assert bare.profile_override is None


def _fabricated_call(profile_override: uuid.UUID | None) -> Call:
    return Call(
        id=uuid.uuid4(),
        elder_id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        attempt=1,
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        profile_override=profile_override,
    )


def test_call_response_echoes_profile_override() -> None:
    pid = uuid.uuid4()
    resp = CallResponse.from_model(_fabricated_call(pid))
    assert resp.profile_override == pid

    resp_none = CallResponse.from_model(_fabricated_call(None))
    assert resp_none.profile_override is None
