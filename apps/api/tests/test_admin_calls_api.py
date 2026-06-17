"""GET /v1/admin/calls — paged, filtered, masked, audited call list (spec §4.1).

Uses the cookie-jar admin_session fixture like test_admin_tools_api; reserved-prefix
(sched:/batch:) and inbound calls are seeded via the repos directly because the
public enqueue 422s on the reserved namespace and never creates inbound rows.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api import object_storage, phi_audit
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.settings import get_settings

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


def _create_contact(client, *, name: str = "Ada") -> tuple[str, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    r = client.post(
        "/v1/contacts",
        json={"name": name, "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"], phone


def _enqueue(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"op-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


async def _seed_admin_user(async_database_url: str, email: str) -> None:
    """Seed an identity-only admin_users row (role moved to memberships, P2 / 0033)."""
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, status, added_by) "
                    "VALUES (:e, 'active', 'test') "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def _as_viewer(client, async_database_url: str) -> None:
    # test_admin_users_api viewer pattern: seed an allow-listed viewer + cookie.
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole
    from usan_api.settings import get_settings

    asyncio.run(_seed_admin_user(async_database_url, "viewer@example.com"))
    token = issue_session(
        "viewer@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _seed_call(
    async_database_url: str,
    *,
    contact_id: str,
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
                    contact_id=uuid.UUID(contact_id),
                    direction=direction,
                    status=status,
                    idempotency_key=idempotency_key,
                )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed_inbound_call(async_database_url: str, *, contact_id: str) -> str:
    async def _go() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                call = await calls_repo.create_inbound_call(
                    db, contact_id=uuid.UUID(contact_id), livekit_room=f"room-in-{uuid.uuid4()}"
                )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed_recorded_call_with_transcript(async_database_url: str, *, contact_id: str) -> str:
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
                    contact_id=uuid.UUID(contact_id),
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


def _delete_contact(async_database_url: str, contact_id: str) -> None:
    async def _go() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM contacts WHERE id = CAST(:id AS uuid)"), {"id": contact_id}
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
    contact_id, phone = _create_contact(client)
    bare_id = _enqueue(client, contact_id)
    recorded_id = _seed_recorded_call_with_transcript(async_database_url, contact_id=contact_id)

    r = client.get("/v1/admin/calls")
    assert r.status_code == 200
    rows = {item["id"]: item for item in r.json()}
    assert {bare_id, recorded_id} <= set(rows)

    for item in (rows[bare_id], rows[recorded_id]):
        assert item["masked_phone"] == "***" + phone[-4:]
        assert item["contact_name"] == "Ada"
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
    contact_a, _phone_a = _create_contact(client, name="Contact A")
    contact_b, _phone_b = _create_contact(client, name="Contact B")

    sched_id = _seed_call(
        async_database_url,
        contact_id=contact_a,
        status=CallStatus.COMPLETED,
        idempotency_key=f"sched:{contact_a}:2026-06-10",
    )
    batch_id = _seed_call(
        async_database_url, contact_id=contact_a, idempotency_key=f"batch:{uuid.uuid4()}:0"
    )
    operator_id = _enqueue(client, contact_a)
    null_key_id = _seed_call(async_database_url, contact_id=contact_b)
    inbound_id = _seed_inbound_call(async_database_url, contact_id=contact_b)

    def ids(query: str) -> list[str]:
        r = client.get(f"/v1/admin/calls{query}")
        assert r.status_code == 200, r.text
        return [item["id"] for item in r.json()]

    # status / direction / contact_id narrow.
    assert ids("?status=completed") == [sched_id]
    assert ids("?direction=inbound") == [inbound_id]
    assert set(ids(f"?contact_id={contact_a}")) == {sched_id, batch_id, operator_id}

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
    contact_id, _phone = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    rows = {item["id"]: item for item in client.get("/v1/admin/calls").json()}
    created_at = _parse_created_at(rows[call_id]["created_at"])
    naive_before = (created_at - timedelta(seconds=1)).replace(tzinfo=None)
    naive_after = (created_at + timedelta(seconds=1)).replace(tzinfo=None)
    included = client.get("/v1/admin/calls", params={"created_from": naive_before.isoformat()})
    assert call_id in [item["id"] for item in included.json()]
    excluded = client.get("/v1/admin/calls", params={"created_from": naive_after.isoformat()})
    assert call_id not in [item["id"] for item in excluded.json()]


def test_admin_calls_list_contact_deleted_unknown(
    client, mock_dispatch, admin_session, async_database_url
):
    contact_id, _phone = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    _delete_contact(async_database_url, contact_id)

    rows = {item["id"]: item for item in client.get("/v1/admin/calls").json()}
    assert rows[call_id]["masked_phone"] == "unknown"
    assert rows[call_id]["contact_name"] is None
    assert rows[call_id]["contact_id"] is None  # FK is ON DELETE SET NULL


def test_admin_calls_list_audit_row_phi_free(
    client, mock_dispatch, admin_session, async_database_url
):
    contact_id, phone = _create_contact(client, name="Audit Sentinel Contact")
    _enqueue(client, contact_id)

    assert client.get("/v1/admin/calls").status_code == 200

    audit_rows = client.get("/v1/admin/audit?action=calls.list").json()
    assert audit_rows, "calls.list read must write an audit entry"
    entry = audit_rows[0]
    assert entry["entity_type"] == "call"
    assert entry["entity_id"] is None
    # Spec §4.1 detail shape: exactly the seven filter values + count, no limit.
    assert set(entry["detail"].keys()) == {
        "contact_id",
        "status",
        "direction",
        "origin",
        "created_from",
        "created_to",
        "offset",
        "count",
    }
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "audit sentinel contact" not in blob
    assert "sentinel" not in blob
    assert phone not in blob
    assert phone[-4:] not in blob


# ---------------------------------------------------------------------------
# B6: GET /v1/admin/calls/{call_id} — detail + transcript + clamped recording URL
# ---------------------------------------------------------------------------

SIGNED_SENTINEL = "https://storage.example/SIGNED-SENTINEL"


def _seed_detail_call(
    async_database_url: str,
    *,
    contact_id: str,
    idempotency_key: str | None = None,
    recording_uri: str | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> str:
    """Seed a completed call with optional reserved key, recording, and transcript."""

    async def _go() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                room = f"room-detail-{uuid.uuid4()}"
                call = await calls_repo.create_call(
                    db,
                    contact_id=uuid.UUID(contact_id),
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.COMPLETED,
                    idempotency_key=idempotency_key,
                    livekit_room=room,
                )
                if recording_uri is not None:
                    recorded = await calls_repo.set_recording_uri(db, room, recording_uri)
                    assert recorded is not None
                if segments:
                    await transcripts_repo.create_transcript_segments(
                        db, call_id=call.id, segments=segments
                    )
                await db.commit()
                return str(call.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _capture_logs(client, url: str, *, level: str = "INFO") -> tuple[int, list[dict]]:
    """GET `url` with a loguru capture installed; returns (status_code, records)."""
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level=level)
    try:
        response = client.get(url)
    finally:
        logger.remove(handler_id)
    return response.status_code, records


def test_admin_call_detail_requires_session(client):
    # §9 auth matrix: the list-level 401 does not pin the detail route — a per-route
    # dependency refactor must not silently drop the session gate here.
    assert client.get(f"/v1/admin/calls/{uuid.uuid4()}").status_code == 401


def test_admin_call_detail_404(client, admin_session):
    r = client.get(f"/v1/admin/calls/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "call not found"
    assert client.get("/v1/admin/calls/not-a-uuid").status_code == 422


def test_admin_call_detail_transcript_and_fields(client, admin_session, async_database_url):
    contact_id, phone = _create_contact(client, name="Detail Contact")
    base = datetime.now(UTC)
    call_id = _seed_detail_call(
        async_database_url,
        contact_id=contact_id,
        idempotency_key=f"sched:{uuid.uuid4()}:2026-06-10",
        # Seeded out of conversation order: the user turn at +2s is inserted LAST, so
        # an insertion-order (id-only) read would misplace it — (started_at, id) wins.
        segments=[
            {"role": "assistant", "content": "Hi, how are you today?", "started_at": base},
            {
                "role": "tool",
                "content": "flagged",
                "tool_name": "flag_for_follow_up",
                "tool_args": {"severity": "routine"},
                "started_at": base + timedelta(seconds=5),
            },
            {"role": "user", "content": "Doing fine.", "started_at": base + timedelta(seconds=2)},
        ],
    )

    r = client.get(f"/v1/admin/calls/{call_id}")
    assert r.status_code == 200
    body = r.json()

    # B5 summary fields plus the detail extras.
    assert body["id"] == call_id
    assert body["contact_name"] == "Detail Contact"
    assert body["masked_phone"] == "***" + phone[-4:]
    assert body["livekit_room"].startswith("room-detail-")
    assert body["parent_call_id"] is None
    assert body["recording_status"] is None
    assert "scheduled_at" in body
    assert "answered_at" in body
    # §9 maps origin parsing to the detail endpoint too (same _summary helper).
    assert body["origin"]["source"] == "schedule"
    assert body["origin"]["ordinal"] == "2026-06-10"

    transcript = body["transcript"]
    assert [seg["role"] for seg in transcript] == ["assistant", "user", "tool"]
    assert transcript[0]["content"] == "Hi, how are you today?"
    assert transcript[0]["tool_name"] is None
    assert transcript[2]["tool_name"] == "flag_for_follow_up"
    assert transcript[2]["tool_args"] == {"severity": "routine"}

    # Deliberately omitted keys (spec §4.2): each is a gratuitous exposure.
    for forbidden in ("dynamic_vars", "error", "idempotency_key", "recording_uri"):
        assert forbidden not in body


def test_admin_call_detail_presigned_url_clamped(
    client, admin_session, async_database_url, monkeypatch
):
    contact_id, _phone = _create_contact(client)
    call_id = _seed_detail_call(
        async_database_url, contact_id=contact_id, recording_uri="gs://test-bucket/detail.ogg"
    )
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    def _sign(gs_uri, ttl, *, expected_bucket=None):
        captured.update(gs_uri=gs_uri, ttl=ttl, expected_bucket=expected_bucket)
        return SIGNED_SENTINEL

    monkeypatch.setattr(object_storage, "generate_signed_url", _sign)

    r = client.get(f"/v1/admin/calls/{call_id}")
    assert r.status_code == 200
    body = r.json()

    # Admin-plane TTL ceiling: settings default 3600 is the MAX of its range, so the
    # clamp must bite — min(3600, 600) == 600 reaches the signer.
    assert captured["ttl"] == min(get_settings().recording_signed_url_ttl_s, 600) == 600
    assert captured["expected_bucket"] == "test-bucket"
    assert captured["gs_uri"] == "gs://test-bucket/detail.ogg"
    assert body["presigned_recording_url"] == SIGNED_SENTINEL
    assert body["recording_url_ttl_s"] == 600


def test_admin_call_detail_signing_failure_200_null(
    client, admin_session, async_database_url, monkeypatch
):
    contact_id, _phone = _create_contact(client)
    call_id = _seed_detail_call(
        async_database_url, contact_id=contact_id, recording_uri="gs://test-bucket/fail.ogg"
    )
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()

    def _boom(gs_uri, ttl, *, expected_bucket=None):
        raise RuntimeError("signBlob unavailable")

    monkeypatch.setattr(object_storage, "generate_signed_url", _boom)

    status_code, records = _capture_logs(client, f"/v1/admin/calls/{call_id}", level="WARNING")
    assert status_code == 200  # the page still renders; the WARN is the operator signal
    r = client.get(f"/v1/admin/calls/{call_id}")
    body = r.json()
    assert body["presigned_recording_url"] is None
    assert body["recording_url_ttl_s"] is None
    assert any(rec["message"] == "Failed to sign recording URL" for rec in records)


def test_admin_call_detail_locked_sink_lines(
    client, admin_session, async_database_url, monkeypatch
):
    contact_id, _phone = _create_contact(client)
    base = datetime.now(UTC)
    call_id = _seed_detail_call(
        async_database_url,
        contact_id=contact_id,
        recording_uri="gs://test-bucket/sink.ogg",
        segments=[
            {"role": "user", "content": "PHI-SENTINEL-DETAIL", "started_at": base},
            {
                "role": "assistant",
                "content": "PHI-SENTINEL-REPLY",
                "started_at": base + timedelta(seconds=1),
            },
        ],
    )
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()
    monkeypatch.setattr(
        object_storage,
        "generate_signed_url",
        lambda gs_uri, ttl, *, expected_bucket=None: SIGNED_SENTINEL,
    )

    status_code, records = _capture_logs(client, f"/v1/admin/calls/{call_id}")
    assert status_code == 200

    transcript_records = [r for r in records if r["message"] == phi_audit.TRANSCRIPT_ACCESSED]
    recording_records = [r for r in records if r["message"] == phi_audit.RECORDING_URL_ACCESSED]
    assert len(transcript_records) == 1
    assert len(recording_records) == 1

    t_extra = transcript_records[0]["extra"]
    assert t_extra["call_id"] == call_id
    assert t_extra["client"]
    assert t_extra["segments"] == 2
    assert t_extra["actor"] == "admin@example.com"

    r_extra = recording_records[0]["extra"]
    assert r_extra["has_recording"] is True
    assert r_extra["actor"] == "admin@example.com"

    # Non-vacuous negatives: the transcript content and the REAL sentinel URL both
    # exist on this request and must appear in no record's message or extra — the
    # URL is a bearer secret, the content is PHI.
    for rec in records:
        blob = rec["message"] + str(rec["extra"])
        assert "PHI-SENTINEL-" not in blob
        assert "SIGNED-SENTINEL" not in blob


def test_admin_call_detail_empty_transcript_no_sink_line(client, admin_session, async_database_url):
    contact_id, _phone = _create_contact(client)
    call_id = _seed_detail_call(async_database_url, contact_id=contact_id)

    status_code, records = _capture_logs(client, f"/v1/admin/calls/{call_id}")
    assert status_code == 200
    # Operator-plane parity: an empty transcript emits no locked-sink line.
    assert not any(r["message"] == phi_audit.TRANSCRIPT_ACCESSED for r in records)


def test_admin_call_detail_audit_row(client, admin_session, async_database_url):
    contact_id, phone = _create_contact(client, name="Audit Detail Contact")
    base = datetime.now(UTC)
    # recording_uri set but no GCS_BUCKET: no URL is signed, yet has_recording=true
    # must still be audited (it reflects the row, not the signing outcome).
    call_id = _seed_detail_call(
        async_database_url,
        contact_id=contact_id,
        recording_uri="gs://test-bucket/audit.ogg",
        segments=[
            {"role": "user", "content": "PHI-SENTINEL-AUDIT", "started_at": base},
            {"role": "assistant", "content": "ok", "started_at": base + timedelta(seconds=1)},
            {"role": "user", "content": "bye", "started_at": base + timedelta(seconds=2)},
        ],
    )

    assert client.get(f"/v1/admin/calls/{call_id}").status_code == 200

    audit_rows = client.get("/v1/admin/audit?action=calls.get").json()
    assert audit_rows, "calls.get read must write an audit entry"
    entry = audit_rows[0]
    assert entry["actor_email"] == "admin@example.com"
    assert entry["entity_type"] == "call"
    assert entry["entity_id"] == call_id
    assert entry["detail"] == {"segments": 3, "has_recording": True}
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "sentinel" not in blob
    assert "audit detail contact" not in blob
    assert phone not in blob
    assert phone[-4:] not in blob


def test_admin_call_detail_audit_failure_rolls_back(
    client, admin_session, async_database_url, monkeypatch
):
    contact_id, _phone = _create_contact(client)
    call_id = _seed_detail_call(async_database_url, contact_id=contact_id)

    from usan_api.routers import admin_calls as admin_calls_router

    real_record = admin_calls_router.admin_audit.record
    attempts = {"n": 0}

    # Raise-once wrapper: a permanently-raising mock would also fail the recovery
    # GET below and make the not-left-dirty assertion impossible.
    async def _raise_once(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SQLAlchemyError("audit write failed")
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(admin_calls_router.admin_audit, "record", _raise_once)

    # The house guard rolls back and re-raises; TestClient re-raises server errors.
    with pytest.raises(SQLAlchemyError):
        client.get(f"/v1/admin/calls/{call_id}")

    # Recovery with the wrapper still installed: the session was not left dirty.
    assert client.get(f"/v1/admin/calls/{call_id}").status_code == 200
    assert attempts["n"] == 2


def test_admin_call_detail_viewer_readable(client, async_database_url):
    # Policy, not accident (spec §6.4): the viewer role — the nurses doing triage —
    # can read transcripts on the detail page.
    contact_id, _phone = _create_contact(client)
    call_id = _seed_detail_call(
        async_database_url,
        contact_id=contact_id,
        segments=[{"role": "user", "content": "viewer-visible", "started_at": datetime.now(UTC)}],
    )
    _as_viewer(client, async_database_url)

    r = client.get(f"/v1/admin/calls/{call_id}")
    assert r.status_code == 200
    assert [seg["content"] for seg in r.json()["transcript"]] == ["viewer-visible"]
