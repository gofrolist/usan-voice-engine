"""GET /v1/admin/calls — paged, filtered, masked, audited call list (spec §4.1).

Uses the cookie-jar admin_session fixture like test_admin_tools_api; reserved-prefix
(sched:/batch:) and inbound calls are seeded via the repos directly because the
public enqueue 422s on the reserved namespace and never creates inbound rows.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import transcripts as transcripts_repo

FORBIDDEN_ITEM_KEYS = {
    "transcript",
    "presigned_recording_url",
    "recording_uri",
    "dynamic_vars",
    "idempotency_key",
}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _create_elder(client, *, name: str = "Ada") -> tuple[str, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    r = client.post(
        "/v1/elders",
        json={"name": name, "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"], phone


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"op-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


async def _seed_admin_user(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "r": role},
            )
    finally:
        await engine.dispose()


def _as_viewer(client, async_database_url: str) -> None:
    # test_admin_users_api viewer pattern: seed an allow-listed viewer + cookie.
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole
    from usan_api.settings import get_settings

    asyncio.run(_seed_admin_user(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _seed_call(
    async_database_url: str,
    *,
    elder_id: str,
    direction: CallDirection = CallDirection.OUTBOUND,
    status: CallStatus = CallStatus.QUEUED,
    idempotency_key: str | None = None,
) -> str:
    """Direct-repo seed: the public enqueue rejects the reserved sched:/batch: prefixes."""

    async def _go() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                call = await calls_repo.create_call(
                    db,
                    elder_id=uuid.UUID(elder_id),
                    direction=direction,
                    status=status,
                    idempotency_key=idempotency_key,
                )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed_inbound_call(async_database_url: str, *, elder_id: str) -> str:
    async def _go() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                call = await calls_repo.create_inbound_call(
                    db, elder_id=uuid.UUID(elder_id), livekit_room=f"room-in-{uuid.uuid4()}"
                )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed_recorded_call_with_transcript(async_database_url: str, *, elder_id: str) -> str:
    """A call carrying both PHI surfaces the list must never leak: a gs:// recording
    URI and a transcript segment with sentinel content."""

    async def _go() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                room = f"room-phi-{uuid.uuid4()}"
                call = await calls_repo.create_call(
                    db,
                    elder_id=uuid.UUID(elder_id),
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.COMPLETED,
                    livekit_room=room,
                )
                recorded = await calls_repo.set_recording_uri(db, room, "gs://test-bucket/phi.ogg")
                assert recorded is not None
                await transcripts_repo.create_transcript_segments(
                    db,
                    call_id=call.id,
                    segments=[
                        {
                            "role": "user",
                            "content": "PHI-SENTINEL-LIST",
                            "started_at": datetime.now(UTC),
                        }
                    ],
                )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _delete_elder(async_database_url: str, elder_id: str) -> None:
    async def _go() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM elders WHERE id = CAST(:id AS uuid)"), {"id": elder_id}
                )
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _parse_created_at(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def test_admin_calls_requires_session(client):
    assert client.get("/v1/admin/calls").status_code == 401


def test_admin_calls_viewer_readable(client, async_database_url):
    # Explicit access policy (spec §1.1/§6.4): reads are session-gated, viewer OK.
    _as_viewer(client, async_database_url)
    assert client.get("/v1/admin/calls").status_code == 200


def test_admin_calls_list_shape_phi_free(client, mock_dispatch, admin_session, async_database_url):
    elder_id, phone = _create_elder(client)
    bare_id = _enqueue(client, elder_id)
    recorded_id = _seed_recorded_call_with_transcript(async_database_url, elder_id=elder_id)

    r = client.get("/v1/admin/calls")
    assert r.status_code == 200
    rows = {item["id"]: item for item in r.json()}
    assert {bare_id, recorded_id} <= set(rows)

    for item in (rows[bare_id], rows[recorded_id]):
        assert item["masked_phone"] == "***" + phone[-4:]
        assert item["elder_name"] == "Ada"
        assert item["direction"] == "outbound"
        assert item["attempt"] == 1
        assert item["created_at"]
        assert not (set(item) & FORBIDDEN_ITEM_KEYS)

    assert rows[bare_id]["status"] == "dialing"  # the enqueue path dials immediately
    assert rows[recorded_id]["status"] == "completed"
    assert rows[bare_id]["has_recording"] is False
    assert rows[recorded_id]["has_recording"] is True

    # Non-vacuous PHI negatives: the recording URI and transcript content exist in
    # the DB for the recorded row and must not pass through the list body.
    assert phone not in r.text
    assert "gs://" not in r.text
    assert "PHI-SENTINEL-LIST" not in r.text


def test_admin_calls_list_filters_paging_ordering(
    client, mock_dispatch, admin_session, async_database_url
):
    elder_a, _phone_a = _create_elder(client, name="Elder A")
    elder_b, _phone_b = _create_elder(client, name="Elder B")

    sched_id = _seed_call(
        async_database_url,
        elder_id=elder_a,
        status=CallStatus.COMPLETED,
        idempotency_key=f"sched:{elder_a}:2026-06-10",
    )
    batch_id = _seed_call(
        async_database_url, elder_id=elder_a, idempotency_key=f"batch:{uuid.uuid4()}:0"
    )
    operator_id = _enqueue(client, elder_a)
    null_key_id = _seed_call(async_database_url, elder_id=elder_b)
    inbound_id = _seed_inbound_call(async_database_url, elder_id=elder_b)

    def ids(query: str) -> list[str]:
        r = client.get(f"/v1/admin/calls{query}")
        assert r.status_code == 200, r.text
        return [item["id"] for item in r.json()]

    # status / direction / elder_id narrow.
    assert ids("?status=completed") == [sched_id]
    assert ids("?direction=inbound") == [inbound_id]
    assert set(ids(f"?elder_id={elder_a}")) == {sched_id, batch_id, operator_id}

    # The B4 origin matrix through HTTP: adhoc = outbound non-reserved keys + NULL
    # keys, NEVER the inbound NULL-key row.
    assert ids("?origin=schedule") == [sched_id]
    assert ids("?origin=batch") == [batch_id]
    assert set(ids("?origin=adhoc")) == {operator_id, null_key_id}

    # Body `origin` parses for the reserved keys and is null for operator keys.
    rows = {item["id"]: item for item in client.get("/v1/admin/calls").json()}
    assert rows[sched_id]["origin"]["source"] == "schedule"
    assert rows[sched_id]["origin"]["ordinal"] == "2026-06-10"
    assert rows[batch_id]["origin"]["source"] == "batch"
    assert rows[operator_id]["origin"] is None
    assert rows[null_key_id]["origin"] is None

    # Paging in (created_at DESC, id DESC): seeded in distinct txns => newest first.
    all_ids = ids("")
    assert all_ids == [inbound_id, null_key_id, operator_id, batch_id, sched_id]
    assert ids("?limit=2") == all_ids[:2]
    assert ids("?limit=2&offset=2") == all_ids[2:4]

    # created_to is EXCLUSIVE through HTTP (§9: the router boundary is where an
    # inclusive/exclusive or kwarg-swap bug would hide from the repo test).
    created_at = _parse_created_at(rows[sched_id]["created_at"])
    excl = client.get("/v1/admin/calls", params={"created_to": created_at.isoformat()})
    assert excl.status_code == 200
    assert sched_id not in [item["id"] for item in excl.json()]
    incl = client.get(
        "/v1/admin/calls",
        params={"created_to": (created_at + timedelta(seconds=1)).isoformat()},
    )
    assert incl.status_code == 200
    assert sched_id in [item["id"] for item in incl.json()]


def test_admin_calls_list_422s(client, mock_dispatch, admin_session):
    assert client.get("/v1/admin/calls?status=notastatus").status_code == 422

    r = client.get(
        "/v1/admin/calls?created_from=2026-06-11T00:00:00&created_to=2026-06-10T00:00:00"
    )
    assert r.status_code == 422

    # Naive datetimes are accepted and assumed UTC: a naive created_from filters as
    # its UTC instant (a non-UTC interpretation shifts the boundary by hours and
    # flips at least one of the two assertions below).
    elder_id, _phone = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    rows = {item["id"]: item for item in client.get("/v1/admin/calls").json()}
    created_at = _parse_created_at(rows[call_id]["created_at"])
    naive_before = (created_at - timedelta(seconds=1)).replace(tzinfo=None)
    naive_after = (created_at + timedelta(seconds=1)).replace(tzinfo=None)
    included = client.get("/v1/admin/calls", params={"created_from": naive_before.isoformat()})
    assert call_id in [item["id"] for item in included.json()]
    excluded = client.get("/v1/admin/calls", params={"created_from": naive_after.isoformat()})
    assert call_id not in [item["id"] for item in excluded.json()]


def test_admin_calls_list_elder_deleted_unknown(
    client, mock_dispatch, admin_session, async_database_url
):
    elder_id, _phone = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    _delete_elder(async_database_url, elder_id)

    rows = {item["id"]: item for item in client.get("/v1/admin/calls").json()}
    assert rows[call_id]["masked_phone"] == "unknown"
    assert rows[call_id]["elder_name"] is None
    assert rows[call_id]["elder_id"] is None  # FK is ON DELETE SET NULL


def test_admin_calls_list_audit_row_phi_free(
    client, mock_dispatch, admin_session, async_database_url
):
    elder_id, phone = _create_elder(client, name="Audit Sentinel Elder")
    _enqueue(client, elder_id)

    assert client.get("/v1/admin/calls").status_code == 200

    audit_rows = client.get("/v1/admin/audit?action=calls.list").json()
    assert audit_rows, "calls.list read must write an audit entry"
    entry = audit_rows[0]
    assert entry["entity_type"] == "call"
    assert entry["entity_id"] is None
    # Spec §4.1 detail shape: exactly the seven filter values + count, no limit.
    assert set(entry["detail"].keys()) == {
        "elder_id",
        "status",
        "direction",
        "origin",
        "created_from",
        "created_to",
        "offset",
        "count",
    }
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "audit sentinel elder" not in blob
    assert "sentinel" not in blob
    assert phone not in blob
    assert phone[-4:] not in blob
