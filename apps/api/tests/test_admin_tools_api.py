"""Admin read endpoint GET /v1/admin/follow-up-flags (session-gated + audited).

Uses the cookie-jar admin_session fixture exactly like test_admin_elders_api /
test_variable_catalog_api (the cookie is set on the shared `client`).
"""

import uuid

import pytest

from tests.conftest import OPERATOR_HEADERS as _OP
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
