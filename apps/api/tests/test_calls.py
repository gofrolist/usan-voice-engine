import uuid
from unittest.mock import AsyncMock

import pytest

from usan_api import livekit_dispatch

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).
_OP = {"Authorization": "Bearer " + "o" * 32}


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={"name": "Ada", "phone_e164": "+15551234567", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    scheduled: list = []
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)

    def _schedule(call_id, settings):
        scheduled.append(call_id)

    from usan_api import dialer

    monkeypatch.setattr(dialer, "schedule_dial", _schedule)
    agent.scheduled = scheduled
    return agent


def test_enqueue_call_dispatches_and_returns_202(client, mock_dispatch):
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "k1", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    body = r.json()
    assert body["direction"] == "outbound"
    assert body["status"] == "dialing"
    mock_dispatch.assert_awaited_once()  # agent dispatched
    assert len(mock_dispatch.scheduled) == 1  # background dial scheduled


def test_enqueue_call_idempotent_replay_returns_200(client, mock_dispatch):
    elder_id = _create_elder(client)
    r1 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
        headers=_OP,
    )
    r2 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert len(mock_dispatch.scheduled) == 1


def test_enqueue_call_conflicting_idempotency_returns_409(client, mock_dispatch):
    elder_id = _create_elder(client)
    first = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 1}},
        headers=_OP,
    )
    assert first.status_code == 202
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 2}},
        headers=_OP,
    )
    assert r.status_code == 409


def test_enqueue_call_dnc_blocked(client, mock_dispatch):
    elder_id = _create_elder(client)
    assert (
        client.post(
            "/v1/dnc", json={"phone_e164": "+15551234567", "reason": "test"}, headers=_OP
        ).status_code
        == 201
    )
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "d1", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dnc_blocked"
    assert mock_dispatch.scheduled == []


def test_enqueue_call_unknown_elder_returns_404(client, mock_dispatch):
    r = client.post(
        "/v1/calls",
        json={
            "elder_id": str(uuid.uuid4()),
            "idempotency_key": "z",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 404


def test_get_call_returns_status(client, mock_dispatch):
    elder_id = _create_elder(client)
    created = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "g1", "dynamic_vars": {}},
        headers=_OP,
    )
    call_id = created.json()["id"]
    r = client.get(f"/v1/calls/{call_id}", headers=_OP)
    assert r.status_code == 200
    assert r.json()["id"] == call_id


def test_enqueue_call_dispatch_config_error_returns_503(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise livekit_dispatch.OutboundDispatchError(
            "not configured: set LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
        )

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 503
    assert "LIVEKIT_SIP_OUTBOUND_TRUNK_ID" not in r.json()["detail"]
    replay = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
        headers=_OP,
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"


def test_enqueue_call_unexpected_dispatch_error_returns_502(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err502", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 502


def test_enqueue_call_idempotency_race_returns_200(client, mock_dispatch, monkeypatch):
    # Simulate the TOCTOU: the early idempotency SELECT misses, so the handler
    # attempts an INSERT that hits the UNIQUE constraint, and the IntegrityError
    # path must re-fetch and return the existing row (200) rather than 500.
    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    first = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "race", "dynamic_vars": {}},
        headers=_OP,
    )
    assert first.status_code == 202

    real = calls_repo.get_by_idempotency_key
    state = {"n": 0}

    async def flaky(db, key):
        state["n"] += 1
        if state["n"] == 1:
            return None  # first (early-check) lookup misses the committed row
        return await real(db, key)

    monkeypatch.setattr(calls_repo, "get_by_idempotency_key", flaky)
    second = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "race", "dynamic_vars": {}},
        headers=_OP,
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_enqueue_call_idempotency_race_conflict_returns_409(client, mock_dispatch, monkeypatch):
    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    first = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "race2", "dynamic_vars": {"a": 1}},
        headers=_OP,
    )
    assert first.status_code == 202

    real = calls_repo.get_by_idempotency_key
    state = {"n": 0}

    async def flaky(db, key):
        state["n"] += 1
        return None if state["n"] == 1 else await real(db, key)

    monkeypatch.setattr(calls_repo, "get_by_idempotency_key", flaky)
    second = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "race2", "dynamic_vars": {"a": 2}},
        headers=_OP,
    )
    assert second.status_code == 409


def test_enqueue_call_acquires_phone_advisory_lock(client, mock_dispatch, monkeypatch):
    # Guard M2: the enqueue gate must take the per-phone advisory lock that
    # serializes it against a concurrent add_dnc for the same number.
    from usan_api.repositories import dnc as dnc_repo

    seen: list[str] = []
    real = dnc_repo.lock_phone

    async def spy(db, phone):
        seen.append(phone)
        await real(db, phone)

    monkeypatch.setattr(dnc_repo, "lock_phone", spy)
    elder_id = _create_elder(client)  # phone +15551234567
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "lock", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    assert seen == ["+15551234567"]


def test_enqueue_call_oversized_dynamic_vars_returns_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    big = {"k": "x" * 9000}
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "big", "dynamic_vars": big},
        headers=_OP,
    )
    assert r.status_code == 422
    mock_dispatch.assert_not_awaited()


def test_enqueue_call_status_is_dialing(client, mock_dispatch):
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dl", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    assert r.json()["status"] == "dialing"


def test_get_unknown_call_returns_404(client):
    r = client.get(f"/v1/calls/{uuid.uuid4()}", headers=_OP)
    assert r.status_code == 404


def test_enqueue_call_requires_operator_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    payload = {"elder_id": elder_id, "idempotency_key": "noauth", "dynamic_vars": {}}
    assert client.post("/v1/calls", json=payload).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    assert client.post("/v1/calls", json=payload, headers=wrong).status_code == 401


def test_get_call_requires_operator_token(client, mock_dispatch):
    r = client.get(f"/v1/calls/{uuid.uuid4()}")
    assert r.status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    assert client.get(f"/v1/calls/{uuid.uuid4()}", headers=wrong).status_code == 401


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    import time

    import jwt

    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _answered_call(client, async_database_url) -> str:
    """Create a call via the API, then force it to in_progress with a direct write.

    Uses a local NullPool engine (not the production get_session_factory) so the
    write runs cleanly under asyncio.run without the cross-event-loop trap.
    """
    import asyncio
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    created = client.post(
        "/v1/calls",
        json={
            "elder_id": elder_id,
            "idempotency_key": f"vm-{_uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    call_id = created.json()["id"]

    async def _answer() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                await calls_repo.mark_answered(db, _uuid.UUID(call_id), sip_call_id="SCL")
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_answer())
    return call_id


def test_outcome_marks_voicemail_left(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "voicemail_left"


def test_outcome_requires_token(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"})
    assert r.status_code == 401


def test_outcome_token_call_id_mismatch_403(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    wrong = _service_token("00000000-0000-0000-0000-000000000000")
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {wrong}"},
    )
    assert r.status_code == 403


def test_outcome_unknown_call_404(client):
    import uuid

    cid = str(uuid.uuid4())
    r = client.post(
        f"/v1/calls/{cid}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 404


def test_outcome_idempotent_noop_when_already_terminal(client, mock_dispatch, async_database_url):
    # A late/duplicate report on an already-terminal call is a 200 no-op, not an error.
    call_id = _answered_call(client, async_database_url)
    first = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "voicemail_left"

    second = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "voicemail_left"


def _call_id_by_idempotency_key(async_database_url: str, key: str) -> str:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    async def _run() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                call = await calls_repo.get_by_idempotency_key(db, key)
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _count_children(async_database_url: str, parent_id: str) -> int:
    import asyncio
    import uuid as _uuid

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.db.models import Call

    async def _run() -> int:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                result = await db.execute(
                    select(func.count())
                    .select_from(Call)
                    .where(Call.parent_call_id == _uuid.UUID(parent_id))
                )
                return result.scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_outcome_voicemail_schedules_retry(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        f"/v1/calls/{call_id}/outcome",
        json={"outcome": "voicemail_left"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200
    assert _count_children(async_database_url, call_id) == 1  # voicemail_left attempt 1 -> +3h


def test_outcome_noop_does_not_schedule_retry(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    headers = {"Authorization": f"Bearer {_service_token(call_id)}"}
    # First report marks voicemail_left + schedules the one allowed retry.
    client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"}, headers=headers)
    # A duplicate report is a no-op and must NOT schedule a second retry.
    client.post(f"/v1/calls/{call_id}/outcome", json={"outcome": "voicemail_left"}, headers=headers)
    assert _count_children(async_database_url, call_id) == 1  # still exactly one child


def test_enqueue_unexpected_dispatch_error_schedules_retry(client, monkeypatch, async_database_url):
    # NOTE: 502 raises HTTPException -> body is {"detail": ...}, no "id" field.
    # Fetch the call ID via idempotency_key so we can count children.
    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    idem_key = "err502retry"
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": idem_key, "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 502

    call_id = _call_id_by_idempotency_key(async_database_url, idem_key)
    # transient dispatch failure -> failed attempt 1 -> +1min child
    assert _count_children(async_database_url, call_id) == 1


def test_enqueue_config_error_does_not_schedule_retry(client, monkeypatch, async_database_url):
    # NOTE: 503 raises HTTPException -> body is {"detail": ...}, no "id" field.
    # Fetch the call ID via idempotency_key so we can count children.
    async def _raise(*args, **kwargs):
        raise livekit_dispatch.OutboundDispatchError("not configured")

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _raise)
    elder_id = _create_elder(client)
    idem_key = "err503noretry"
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": idem_key, "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 503

    call_id = _call_id_by_idempotency_key(async_database_url, idem_key)
    assert _count_children(async_database_url, call_id) == 0  # misconfig is permanent


def test_enqueue_call_resolves_builtins_into_dispatch_metadata(client, monkeypatch):

    captured = {}

    async def _spy_dispatch(call, *, settings, resolved_vars=None, timezone=""):
        captured["resolved_vars"] = resolved_vars
        captured["timezone"] = timezone
        captured["persisted_dynamic_vars"] = dict(call.dynamic_vars)

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _spy_dispatch)
    from usan_api import dialer

    monkeypatch.setattr(dialer, "schedule_dial", lambda *a, **k: None)

    r = client.post(
        "/v1/elders",
        json={
            "name": "Margaret Doe",
            "phone_e164": "+15557654321",
            "timezone": "US/Eastern",
            "metadata": {"medication_schedule": [{"name": "Lisinopril"}]},
        },
        headers=_OP,
    )
    elder_id = r.json()["id"]
    resp = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "rv1", "dynamic_vars": {"promo": "x"}},
        headers=_OP,
    )
    assert resp.status_code == 202
    assert captured["resolved_vars"]["first_name"] == "Margaret"
    assert captured["resolved_vars"]["call_direction"] == "outbound"
    assert captured["resolved_vars"]["today_meds"] == "Lisinopril"
    assert captured["timezone"] == "US/Eastern"
    # §4.3: built-ins are NOT merged into the persisted idempotency payload.
    assert captured["persisted_dynamic_vars"] == {"promo": "x"}


def test_enqueue_call_idempotent_replay_still_matches_after_builtin_resolution(client, monkeypatch):
    # Built-in resolution must not touch Call.dynamic_vars, so a replay still 200s.
    async def _noop_dispatch(call, *, settings, resolved_vars=None, timezone=""):
        return None

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", _noop_dispatch)
    from usan_api import dialer

    monkeypatch.setattr(dialer, "schedule_dial", lambda *a, **k: None)

    r = client.post(
        "/v1/elders",
        json={"name": "Ada Lovelace", "phone_e164": "+15550008888", "timezone": "UTC"},
        headers=_OP,
    )
    elder_id = r.json()["id"]
    payload = {"elder_id": elder_id, "idempotency_key": "replay-rv", "dynamic_vars": {"a": 1}}
    first = client.post("/v1/calls", json=payload, headers=_OP)
    second = client.post("/v1/calls", json=payload, headers=_OP)
    assert first.status_code == 202
    assert second.status_code == 200  # not 409 — payload unchanged
    assert second.json()["id"] == first.json()["id"]
