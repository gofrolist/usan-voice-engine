import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _make_contact(client, phone):
    return client.post(
        "/v1/admin/contacts",
        json={"name": "Sched Target", "phone_e164": phone, "timezone": _TZ},
    ).json()["id"]


def _create_body(cid):
    return {
        "contact_id": cid,
        "slot": "morning",
        "window_start_local": "09:30:00",
        "window_end_local": "11:00:00",
        "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        "enabled": True,
    }


def test_create_list_get_schedule(client, admin_session):
    cid = _make_contact(client, "+15551231001")
    r = client.post("/v1/admin/schedules", json=_create_body(cid))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["slot"] == "morning"

    listed = client.get("/v1/admin/schedules", params={"contact_id": cid}).json()
    assert any(s["id"] == sid for s in listed)
    assert client.get(f"/v1/admin/schedules/{sid}").status_code == 200


def test_responses_include_contact_name(client, admin_session):
    """Every admin schedule response carries the contact's name so the global
    "who missed" list can show a name instead of a bare UUID (create/list/get/patch)."""
    cid = _make_contact(client, "+15551231005")
    created = client.post("/v1/admin/schedules", json=_create_body(cid))
    assert created.status_code == 201, created.text
    assert created.json()["contact_name"] == "Sched Target"
    sid = created.json()["id"]

    listed = client.get("/v1/admin/schedules", params={"contact_id": cid}).json()
    row = next(s for s in listed if s["id"] == sid)
    assert row["contact_name"] == "Sched Target"

    got = client.get(f"/v1/admin/schedules/{sid}")
    assert got.status_code == 200
    assert got.json()["contact_name"] == "Sched Target"

    patched = client.patch(f"/v1/admin/schedules/{sid}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["contact_name"] == "Sched Target"


def test_duplicate_slot_409(client, admin_session):
    cid = _make_contact(client, "+15551231002")
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 201
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 409


def test_patch_disable_then_delete(client, admin_session):
    cid = _make_contact(client, "+15551231003")
    sid = client.post("/v1/admin/schedules", json=_create_body(cid)).json()["id"]
    r = client.patch(f"/v1/admin/schedules/{sid}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert client.delete(f"/v1/admin/schedules/{sid}").status_code == 204
    assert client.get(f"/v1/admin/schedules/{sid}").status_code == 404


def test_viewer_cannot_create_schedule(client, admin_session, async_database_url):
    cid = _make_contact(client, "+15551231004")
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-sched@example.com", "viewer"))
    token = issue_session(
        "viewer-sched@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 403


def test_schedules_requires_session(client):
    assert client.get("/v1/admin/schedules").status_code == 401
