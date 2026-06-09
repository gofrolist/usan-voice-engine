import time
import uuid

import jwt
import pytest

from tests.conftest import counter_value as _counter_value
from usan_api import livekit_dispatch

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).
_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _create_elder(client, *, metadata: dict | None = None) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": metadata or {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"tool-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def test_log_wellness_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 4, "pain_level": 2, "notes": "feeling good"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_wellness_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
    )
    assert r.status_code == 401


def test_log_wellness_token_call_id_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


def test_log_wellness_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": cid, "mood": 3},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_log_wellness_out_of_range_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 9},  # mood is 1..5
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_log_medication_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_medication_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": False},
    )
    assert r.status_code == 401


def test_log_medication_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


_SCHEDULE = [
    {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]},
    {"name": "Metformin", "times": ["08:00", "20:00"]},
]


def test_get_today_meds_returns_schedule(client, mock_dispatch):
    elder_id = _create_elder(client, metadata={"medication_schedule": _SCHEDULE})
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 2
    assert meds[0] == {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}
    assert meds[1] == {"name": "Metformin", "dosage": None, "times": ["08:00", "20:00"]}


def test_get_today_meds_empty_when_no_schedule(client, mock_dispatch):
    elder_id = _create_elder(client)  # no medication_schedule in meta
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["medications"] == []


def test_get_today_meds_skips_malformed_entries(client, mock_dispatch):
    bad = [{"name": "Good", "times": ["09:00"]}, {"dosage": "x"}, "nonsense"]
    elder_id = _create_elder(client, metadata={"medication_schedule": bad})
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 1
    assert meds[0]["name"] == "Good"


def test_get_today_meds_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def _answered_call(client, async_database_url) -> str:
    """Enqueue a call, then force it to IN_PROGRESS with a direct write."""
    import asyncio as _asyncio
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)

    async def _answer() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                await calls_repo.mark_answered(db, _uuid.UUID(call_id), sip_call_id="SCL")
                await db.commit()
        finally:
            await engine.dispose()

    _asyncio.run(_answer())
    return call_id


def test_end_call_completes_in_progress(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_end_call_idempotent_noop(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    first = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "user_ended"},
        headers=_auth(call_id),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "completed"  # unchanged; reason not overwritten


def test_end_call_mismatch_403(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "x"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_end_call_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": cid, "reason": "x"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_end_call_noop_when_not_in_progress(client, mock_dispatch):
    # A call that hasn't been answered (status "dialing") is not completed by end_call;
    # the handler returns the call's current status unchanged.
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "x"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dialing"


def test_log_transcript_inserts_segments(client, mock_dispatch, async_database_url):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [
                {"role": "assistant", "content": "Hello!", "started_at": "2026-06-01T12:00:00Z"},
                {"role": "user", "content": "I'm good", "started_at": "2026-06-01T12:00:05Z"},
                {
                    "role": "tool",
                    "content": "log_wellness",
                    "tool_name": "log_wellness",
                    "tool_args": {"mood": 4},
                    "started_at": "2026-06-01T12:00:06Z",
                },
            ],
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_log_transcript_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}],
        },
    )
    assert r.status_code == 401


def test_log_transcript_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}],
        },
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_log_transcript_empty_segments_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={"call_id": call_id, "segments": []},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_flag_for_followup_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={
            "call_id": call_id,
            "severity": "urgent",
            "category": "medical",
            "reason": "reported chest pain",
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_flag_for_followup_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "routine", "category": "other", "reason": "x"},
    )
    assert r.status_code == 401


def test_flag_for_followup_call_id_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "routine", "category": "other", "reason": "x"},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


def test_flag_for_followup_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": cid, "severity": "routine", "category": "other", "reason": "x"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_flag_for_followup_bad_enum_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "panic", "category": "medical", "reason": "x"},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_flag_for_followup_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import FOLLOWUP_FLAGS_TOTAL

    before = _counter_value(FOLLOWUP_FLAGS_TOTAL, severity="urgent", category="safety")
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "urgent", "category": "safety", "reason": "fell"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    after = _counter_value(FOLLOWUP_FLAGS_TOTAL, severity="urgent", category="safety")
    assert after == before + 1
