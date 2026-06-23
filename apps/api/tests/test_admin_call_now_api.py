import asyncio
import uuid

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _contact(client, phone):
    return client.post(
        "/v1/admin/contacts",
        json={"name": "Call Target", "phone_e164": phone, "timezone": _TZ},
    ).json()["id"]


def test_call_now_dnc_blocked_returns_blocked(client, admin_session):
    phone = "+15551239001"
    cid = _contact(client, phone)
    # Put the number on the org's DNC list via the operator endpoint (same seeded org).
    assert (
        client.post(
            "/v1/dnc",
            json={"phone_e164": phone, "reason": "test"},
            headers={"Authorization": "Bearer " + "o" * 32},
        ).status_code
        == 201
    )

    r = client.post("/v1/admin/calls", json={"contact_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dnc_blocked"


def test_call_now_unknown_contact_404(client, admin_session):
    assert client.post("/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}).status_code == 404


def test_call_now_viewer_403(client, admin_session, async_database_url):
    cid = _contact(client, "+15551239002")
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-call@example.com", "viewer"))
    token = issue_session(
        "viewer-call@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/v1/admin/calls", json={"contact_id": cid}).status_code == 403


def test_call_now_requires_session(client):
    assert client.post("/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}).status_code == 401
