"""Feature A (spec §3.1): `profile_override` on `POST /v1/calls`.

B1: request/response schema + `create_call` kwarg threading. The column itself
exists since migration 0010 — only the ad-hoc write path is new here.

B2: `enqueue_call` wiring — liveness 422 on the create path, the idempotency
contract (replay pre-check beats liveness; payload match includes the override),
DNC-branch persistence, and the runtime end-to-end resolution pin.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import dialer, livekit_dispatch
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.routers.calls import _idempotent_replay
from usan_api.schemas.call import CallResponse, CreateCallRequest

_OVERRIDE_ERROR = "profile_override must reference an active profile with a published version"


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def _clean(session_factory):
    # Not autouse: the pure schema tests in this module must not pay for Postgres.
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls, agent_profiles, contacts RESTART IDENTITY CASCADE"))
        await db.commit()


def test_create_call_request_profile_override_optional_uuid() -> None:
    contact_id = uuid.uuid4()
    omitted = CreateCallRequest(contact_id=contact_id, idempotency_key="k")
    assert omitted.profile_override is None

    pid = uuid.uuid4()
    given = CreateCallRequest(contact_id=contact_id, idempotency_key="k", profile_override=str(pid))
    assert given.profile_override == pid
    assert isinstance(given.profile_override, uuid.UUID)

    with pytest.raises(ValidationError):
        CreateCallRequest(contact_id=contact_id, idempotency_key="k", profile_override="not-a-uuid")


async def _seed_contact_and_profile(factory) -> tuple[uuid.UUID, uuid.UUID]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Override Contact", phone_e164=phone, timezone="America/New_York"
        )
        profile = await profiles_repo.create_profile(
            db, name="override-profile", description=None, actor_email="admin@example.com"
        )
        await db.commit()
        return contact.id, profile.id


@pytest.mark.usefixtures("_clean")
async def test_create_call_persists_profile_override(session_factory) -> None:
    contact_id, pid = await _seed_contact_and_profile(session_factory)

    async with session_factory() as db:
        with_override = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            profile_override=pid,
        )
        without_override = await calls_repo.create_call(
            db,
            contact_id=contact_id,
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
        contact_id=uuid.uuid4(),
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


# --- B2: enqueue_call wiring (HTTP tests; dispatch mocked per the test_calls.py pattern) ---

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    scheduled: list = []
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)

    def _schedule(call_id, settings):
        scheduled.append(call_id)

    monkeypatch.setattr(dialer, "schedule_dial", _schedule)
    agent.scheduled = scheduled
    return agent


def _seed_contact_http(client) -> tuple[str, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    r = client.post(
        "/v1/contacts",
        json={"name": "Override Contact", "phone_e164": phone, "timezone": "America/New_York"},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"], phone


async def _run_db(async_database_url: str, fn: Callable[[Any], Awaitable[Any]]) -> Any:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            result = await fn(db)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def _seed_live_profile(async_database_url: str) -> str:
    """Create an ACTIVE profile and publish v1 — the liveness precondition."""

    async def _seed(db):
        profile = await profiles_repo.create_profile(
            db, name=f"live-{uuid.uuid4().hex}", description=None, actor_email="op@example.com"
        )
        await profiles_repo.publish(db, profile.id, note="v1", actor_email="op@example.com")
        return str(profile.id)

    return asyncio.run(_run_db(async_database_url, _seed))


def _seed_draft_profile(async_database_url: str) -> str:
    async def _seed(db):
        profile = await profiles_repo.create_profile(
            db, name=f"draft-{uuid.uuid4().hex}", description=None, actor_email="op@example.com"
        )
        return str(profile.id)

    return asyncio.run(_run_db(async_database_url, _seed))


def _archive_profile_raw(async_database_url: str, profile_id: str) -> None:
    async def _archive(db):
        await db.execute(
            text("UPDATE agent_profiles SET status = 'archived' WHERE id = :pid"),
            {"pid": profile_id},
        )

    asyncio.run(_run_db(async_database_url, _archive))


def _db_profile_override(async_database_url: str, call_id: str) -> uuid.UUID | None:
    async def _read(db):
        call = await db.get(Call, uuid.UUID(call_id))
        assert call is not None
        return call.profile_override

    return asyncio.run(_run_db(async_database_url, _read))


def _enqueue(client, contact_id: str, key: str, override: str | None):
    payload: dict[str, Any] = {"contact_id": contact_id, "idempotency_key": key, "dynamic_vars": {}}
    if override is not None:
        payload["profile_override"] = override
    return client.post("/v1/calls", json=payload, headers=_OP)


def test_enqueue_with_live_override_persists_and_echoes(
    client, mock_dispatch, async_database_url
) -> None:
    contact_id, _ = _seed_contact_http(client)
    pid = _seed_live_profile(async_database_url)

    r = _enqueue(client, contact_id, "ov-live", pid)
    assert r.status_code == 202
    body = r.json()
    assert body["profile_override"] == pid
    # Fresh-session read: the row itself carries the override (not just the echo).
    assert _db_profile_override(async_database_url, body["id"]) == uuid.UUID(pid)


@pytest.mark.parametrize("shape", ["draft_only", "archived", "unknown"])
def test_enqueue_422_when_override_not_live(
    client, mock_dispatch, async_database_url, shape
) -> None:
    contact_id, _ = _seed_contact_http(client)
    if shape == "draft_only":
        pid = _seed_draft_profile(async_database_url)
    elif shape == "archived":
        pid = _seed_live_profile(async_database_url)
        _archive_profile_raw(async_database_url, pid)
    else:
        pid = str(uuid.uuid4())

    r = _enqueue(client, contact_id, f"ov-dead-{shape}", pid)
    assert r.status_code == 422
    assert r.json()["detail"] == _OVERRIDE_ERROR
    mock_dispatch.assert_not_awaited()


def test_dnc_path_persists_override(client, mock_dispatch, async_database_url) -> None:
    contact_id, phone = _seed_contact_http(client)
    pid = _seed_live_profile(async_database_url)
    dnc = client.post("/v1/dnc", json={"phone_e164": phone, "reason": "test"}, headers=_OP)
    assert dnc.status_code == 201

    r = _enqueue(client, contact_id, "ov-dnc", pid)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "dnc_blocked"
    assert body["profile_override"] == pid  # echoed
    assert _db_profile_override(async_database_url, body["id"]) == uuid.UUID(pid)  # persisted
    mock_dispatch.assert_not_awaited()


def test_replay_identical_after_override_archived_returns_200(
    client, mock_dispatch, async_database_url
) -> None:
    # The ordering pin (spec §3.1, load-bearing): the replay pre-check beats liveness,
    # so an identical retry-on-timeout replay wins even after the profile was archived.
    contact_id, _ = _seed_contact_http(client)
    pid = _seed_live_profile(async_database_url)

    first = _enqueue(client, contact_id, "ov-replay-arch", pid)
    assert first.status_code == 202

    _archive_profile_raw(async_database_url, pid)

    replay = _enqueue(client, contact_id, "ov-replay-arch", pid)
    assert replay.status_code == 200  # never 422
    assert replay.json()["id"] == first.json()["id"]


@pytest.mark.parametrize("original_has_override", [False, True])
def test_replay_with_different_override_409(
    client, mock_dispatch, async_database_url, original_has_override
) -> None:
    contact_id, _ = _seed_contact_http(client)
    original_override = _seed_live_profile(async_database_url) if original_has_override else None

    first = _enqueue(client, contact_id, "ov-mismatch", original_override)
    assert first.status_code == 202

    identical = _enqueue(client, contact_id, "ov-mismatch", original_override)
    assert identical.status_code == 200
    assert identical.json()["id"] == first.json()["id"]

    mismatched = _enqueue(client, contact_id, "ov-mismatch", str(uuid.uuid4()))
    assert mismatched.status_code == 409
    assert mismatched.json()["detail"] == "idempotency_key reused with a different payload"


def test_idempotent_replay_helper_409_on_override_mismatch() -> None:
    # Unit pin on the helper itself: it has exactly two consumers — the replay
    # pre-check and the IntegrityError race fallback — both get this for free.
    contact_id = uuid.uuid4()
    pid_a, pid_b = uuid.uuid4(), uuid.uuid4()

    def _existing(override: uuid.UUID | None) -> Call:
        call = _fabricated_call(override)
        call.contact_id = contact_id
        call.dynamic_vars = {}
        return call

    def _body(override: uuid.UUID | None) -> CreateCallRequest:
        return CreateCallRequest(
            contact_id=contact_id, idempotency_key="k", profile_override=override
        )

    with pytest.raises(HTTPException) as exc_a:
        _idempotent_replay(_existing(pid_a), _body(pid_b), Response())
    assert exc_a.value.status_code == 409

    with pytest.raises(HTTPException) as exc_b:
        _idempotent_replay(_existing(None), _body(pid_a), Response())
    assert exc_b.value.status_code == 409

    # set→None: a replay that DROPS the original's override is a different
    # payload too — the mismatch check must be symmetric, not set-only.
    with pytest.raises(HTTPException) as exc_c:
        _idempotent_replay(_existing(pid_a), _body(None), Response())
    assert exc_c.value.status_code == 409

    response = Response()
    matched = _idempotent_replay(_existing(pid_a), _body(pid_a), response)
    assert matched.profile_override == pid_a
    assert response.status_code == 200


def test_idempotency_race_fallback_409_on_override_mismatch(
    client, mock_dispatch, async_database_url, monkeypatch
) -> None:
    # The helper's SECOND consumer — the IntegrityError race fallback in
    # _create_and_dispatch — exercised DIRECTLY with an override mismatch
    # (spec §8): the early pre-check is forced to miss (the test_calls.py
    # flaky seam), the INSERT hits the unique key, and the fallback's
    # payload-match must 409, never silently adopt the existing row.
    from usan_api.repositories import calls as repo

    contact_id, _ = _seed_contact_http(client)
    pid_a = _seed_live_profile(async_database_url)
    pid_b = _seed_live_profile(async_database_url)

    first = _enqueue(client, contact_id, "ov-race", pid_a)
    assert first.status_code == 202

    real = repo.get_by_idempotency_key
    state = {"n": 0}

    async def flaky(db, key):
        state["n"] += 1
        return None if state["n"] == 1 else await real(db, key)

    monkeypatch.setattr(repo, "get_by_idempotency_key", flaky)
    second = _enqueue(client, contact_id, "ov-race", pid_b)
    assert second.status_code == 409
    assert second.json()["detail"] == "idempotency_key reused with a different payload"


def _worker_auth() -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, "s" * 32, algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


def test_runtime_config_resolves_adhoc_override_end_to_end(
    client, mock_dispatch, async_database_url
) -> None:
    # Pins the existing runtime.py precedence walk against the new write path: an
    # ad-hoc enqueue's override must be what the worker resolves for the call.
    contact_id, _ = _seed_contact_http(client)
    pid = _seed_live_profile(async_database_url)

    enqueued = _enqueue(client, contact_id, "ov-runtime", pid)
    assert enqueued.status_code == 202
    call_id = enqueued.json()["id"]

    r = client.get(
        "/v1/runtime/agent-config",
        params={"direction": "outbound", "call_id": call_id},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["profile_id"] == pid
