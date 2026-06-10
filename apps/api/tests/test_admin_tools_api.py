"""Admin read endpoint GET /v1/admin/follow-up-flags (session-gated + audited)
plus the C2 PATCH transition matrix for both ops queues (spec §4.3/§9).

Uses the cookie-jar admin_session fixture exactly like test_admin_elders_api /
test_variable_catalog_api (the cookie is set on the shared `client`).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import counter_value
from tests.conftest import service_token as _service_token


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"flag-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def _seed_flag(client, *, severity="urgent", category="medical", reason="reported chest pain"):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": severity, "category": category, "reason": reason},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200, r.text
    return elder_id, call_id, r.json()["id"]


def test_follow_up_flags_requires_session(client):
    assert client.get("/v1/admin/follow-up-flags").status_code == 401


def test_follow_up_flags_list_and_filter(client, mock_dispatch, admin_session):
    elder_id, _call_id, flag_id = _seed_flag(client)

    rows = client.get("/v1/admin/follow-up-flags").json()
    me = next(f for f in rows if f["id"] == flag_id)
    assert me["severity"] == "urgent"
    assert me["category"] == "medical"
    assert me["reason"] == "reported chest pain"
    assert me["status"] == "open"

    by_elder = client.get(f"/v1/admin/follow-up-flags?elder_id={elder_id}").json()
    assert [f["id"] for f in by_elder] == [flag_id]

    open_rows = client.get("/v1/admin/follow-up-flags?status=open").json()
    assert any(f["id"] == flag_id for f in open_rows)
    closed_rows = client.get("/v1/admin/follow-up-flags?status=closed").json()
    assert all(f["id"] != flag_id for f in closed_rows)

    # Over-cap limit rejected by Query(le=...).
    assert client.get("/v1/admin/follow-up-flags?limit=100000").status_code == 422


def test_follow_up_flags_read_is_audited_phi_free(client, mock_dispatch, admin_session):
    # F7: AuditEntryOut serializes `detail`. The admin read of PHI rows must be
    # audited, but the audit entry itself must carry NO PHI (no reason text).
    _elder_id, _call_id, _flag_id = _seed_flag(client, reason="secret chest pain note")
    client.get("/v1/admin/follow-up-flags")
    rows = client.get("/v1/admin/audit?action=follow_up_flags.list").json()
    assert rows, "follow-up-flags read must write an audit entry"
    entry = rows[0]
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "secret" not in blob
    assert "chest pain" not in blob


# --- C2: PATCH transition matrix for both ops queues (spec §4.3/§9) ---


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
    # B5 viewer pattern (test_admin_calls_api): seed an allow-listed viewer + cookie.
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole
    from usan_api.settings import get_settings

    asyncio.run(_seed_admin_user(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)


async def _seed_callback_async(async_database_url: str, *, notes: str | None = None) -> int:
    """Direct-DB callback seed returning the new row id (PATCH target).

    Modeled on test_admin_callback_requests_api._seed_callback; the public tool
    endpoint path is exercised there — here we only need a row to transition.
    """
    elder_id = str(uuid.uuid4())
    call_id = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), 'Ada', :p, 'UTC')"
                ),
                {"id": elder_id, "p": f"+1555{str(uuid.UUID(elder_id).int)[:7].zfill(7)}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'outbound', 'completed')"
                ),
                {"cid": call_id, "eid": elder_id},
            )
            result = await conn.execute(
                text(
                    "INSERT INTO callback_requests "
                    "(call_id, elder_id, requested_time_text, notes) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'tomorrow at 3', :n) "
                    "RETURNING id"
                ),
                {"cid": call_id, "eid": elder_id, "n": notes},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def _seed_callback(async_database_url: str, *, notes: str | None = None) -> int:
    return asyncio.run(_seed_callback_async(async_database_url, notes=notes))


def _audit_count(client, action: str) -> int:
    return len(client.get(f"/v1/admin/audit?action={action}").json())


def test_flag_transition_matrix(client, mock_dispatch, admin_session):
    _elder_id, _call_id, flag_id = _seed_flag(client)

    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "acknowledged"
    assert body["status_updated_by"] == "admin@example.com"
    assert body["status_updated_at"] is not None

    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"

    _e2, _c2, fresh_id = _seed_flag(client)
    r = client.patch(f"/v1/admin/follow-up-flags/{fresh_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"


def test_flag_transition_idempotent_noop(client, mock_dispatch, admin_session):
    _elder_id, _call_id, flag_id = _seed_flag(client)

    first = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert first.status_code == 200, first.text
    stamps = (first.json()["status_updated_at"], first.json()["status_updated_by"])
    audits = _audit_count(client, "follow_up_flag.update")

    # (a) ack -> ack replay: 200, stamps byte-identical, no new audit row.
    replay = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert replay.status_code == 200, replay.text
    assert (replay.json()["status_updated_at"], replay.json()["status_updated_by"]) == stamps
    assert _audit_count(client, "follow_up_flag.update") == audits

    resolved = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "resolved"})
    assert resolved.status_code == 200, resolved.text
    stamps = (resolved.json()["status_updated_at"], resolved.json()["status_updated_by"])
    audits = _audit_count(client, "follow_up_flag.update")

    # (b) resolved -> resolved replay: the likelier real-world double-click
    # (terminal state) — same idempotent contract.
    replay = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "resolved"})
    assert replay.status_code == 200, replay.text
    assert (replay.json()["status_updated_at"], replay.json()["status_updated_by"]) == stamps
    assert _audit_count(client, "follow_up_flag.update") == audits


def test_flag_transition_backward_409(client, mock_dispatch, admin_session):
    _elder_id, _call_id, flag_id = _seed_flag(client)
    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text

    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert r.status_code == 409
    assert r.json()["detail"] == "illegal transition: resolved -> acknowledged"


def test_flag_transition_404_422_401_403(client, mock_dispatch, admin_session, async_database_url):
    _elder_id, _call_id, flag_id = _seed_flag(client)

    r = client.patch("/v1/admin/follow-up-flags/999999", json={"status": "acknowledged"})
    assert r.status_code == 404
    assert r.json()["detail"] == "flag not found"

    # "open" is not a settable target.
    assert (
        client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "open"}).status_code
        == 422
    )

    client.cookies.clear()
    assert (
        client.patch(
            f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"}
        ).status_code
        == 401
    )

    _as_viewer(client, async_database_url)
    assert (
        client.patch(
            f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"}
        ).status_code
        == 403
    )
    # No DB change: the flag is still open (reads are viewer-OK).
    rows = client.get("/v1/admin/follow-up-flags").json()
    me = next(f for f in rows if f["id"] == flag_id)
    assert me["status"] == "open"


def test_flag_transition_audit_from_to_only(client, mock_dispatch, admin_session):
    _elder_id, _call_id, flag_id = _seed_flag(client, reason="secret transition reason")
    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text

    rows = client.get("/v1/admin/audit?action=follow_up_flag.update").json()
    assert rows, "queue transition must write an audit entry"
    entry = rows[0]
    assert entry["detail"] == {"from": "open", "to": "acknowledged"}
    assert entry["entity_type"] == "follow_up_flag"
    assert entry["entity_id"] == str(flag_id)
    blob = str(entry).lower()
    assert "secret" not in blob
    assert "transition reason" not in blob


def test_callback_transition_matrix(client, admin_session, async_database_url):
    cb_id = _seed_callback(async_database_url)

    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "acknowledged"
    assert body["status_updated_by"] == "admin@example.com"
    assert body["status_updated_at"] is not None

    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"

    fresh_id = _seed_callback(async_database_url)
    r = client.patch(f"/v1/admin/callback-requests/{fresh_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"


def test_callback_transition_idempotent_noop(client, admin_session, async_database_url):
    cb_id = _seed_callback(async_database_url)

    first = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"})
    assert first.status_code == 200, first.text
    stamps = (first.json()["status_updated_at"], first.json()["status_updated_by"])
    audits = _audit_count(client, "callback_request.update")

    replay = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"})
    assert replay.status_code == 200, replay.text
    assert (replay.json()["status_updated_at"], replay.json()["status_updated_by"]) == stamps
    assert _audit_count(client, "callback_request.update") == audits

    resolved = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "resolved"})
    assert resolved.status_code == 200, resolved.text
    stamps = (resolved.json()["status_updated_at"], resolved.json()["status_updated_by"])
    audits = _audit_count(client, "callback_request.update")

    replay = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "resolved"})
    assert replay.status_code == 200, replay.text
    assert (replay.json()["status_updated_at"], replay.json()["status_updated_by"]) == stamps
    assert _audit_count(client, "callback_request.update") == audits


def test_callback_transition_backward_409(client, admin_session, async_database_url):
    cb_id = _seed_callback(async_database_url)
    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text

    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"})
    assert r.status_code == 409
    assert r.json()["detail"] == "illegal transition: resolved -> acknowledged"


def test_callback_transition_404_422_401_403(client, admin_session, async_database_url):
    cb_id = _seed_callback(async_database_url)

    r = client.patch("/v1/admin/callback-requests/999999", json={"status": "acknowledged"})
    assert r.status_code == 404
    assert r.json()["detail"] == "request not found"

    assert (
        client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "open"}).status_code
        == 422
    )

    client.cookies.clear()
    assert (
        client.patch(
            f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"}
        ).status_code
        == 401
    )

    _as_viewer(client, async_database_url)
    assert (
        client.patch(
            f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"}
        ).status_code
        == 403
    )
    rows = client.get("/v1/admin/callback-requests").json()
    me = next(r for r in rows if r["id"] == cb_id)
    assert me["status"] == "open"


def test_callback_transition_audit_from_to_only(client, admin_session, async_database_url):
    cb_id = _seed_callback(async_database_url, notes="secret callback transition note")
    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text

    rows = client.get("/v1/admin/audit?action=callback_request.update").json()
    assert rows, "queue transition must write an audit entry"
    entry = rows[0]
    assert entry["detail"] == {"from": "open", "to": "acknowledged"}
    assert entry["entity_type"] == "callback_request"
    assert entry["entity_id"] == str(cb_id)
    blob = str(entry).lower()
    assert "secret" not in blob
    assert "callback transition note" not in blob


def test_transition_metric_after_commit_not_noop(
    client, mock_dispatch, admin_session, async_database_url
):
    # Local import: at RED the counter does not exist yet; module collection must
    # still see the 405/404s on the missing PATCH routes (plan C2 step 2).
    from usan_api.observability.custom_metrics import ADMIN_QUEUE_TRANSITIONS_TOTAL

    _elder_id, _call_id, flag_id = _seed_flag(client)
    before = counter_value(
        ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="follow_up_flag", to_status="acknowledged"
    )
    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text
    assert (
        counter_value(
            ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="follow_up_flag", to_status="acknowledged"
        )
        == before + 1
    )

    # Idempotent replay: 200 no-op, counter unchanged.
    r = client.patch(f"/v1/admin/follow-up-flags/{flag_id}", json={"status": "acknowledged"})
    assert r.status_code == 200, r.text
    assert (
        counter_value(
            ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="follow_up_flag", to_status="acknowledged"
        )
        == before + 1
    )

    # The callback_request label combination must not go unasserted.
    cb_id = _seed_callback(async_database_url)
    cb_before = counter_value(
        ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="callback_request", to_status="resolved"
    )
    r = client.patch(f"/v1/admin/callback-requests/{cb_id}", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert (
        counter_value(ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="callback_request", to_status="resolved")
        == cb_before + 1
    )
