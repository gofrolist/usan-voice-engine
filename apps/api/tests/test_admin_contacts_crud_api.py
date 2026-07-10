import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _viewer_cookie(client, async_database_url, email="viewer-crud@example.com"):
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, email, "viewer"))
    token = issue_session(
        email,
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


def test_create_contact_then_detail(client, admin_session):
    r = client.post(
        "/v1/admin/contacts",
        json={"name": "Grace Hopper", "phone_e164": "+15551230101", "timezone": _TZ},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["masked_phone"].endswith("0101")

    detail = client.get(f"/v1/admin/contacts/{cid}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Grace Hopper"
    assert "phone_e164" not in detail.json()  # masked only, never raw


def test_create_duplicate_phone_409(client, admin_session):
    body = {"name": "Dup", "phone_e164": "+15551230202", "timezone": _TZ}
    assert client.post("/v1/admin/contacts", json=body).status_code == 201
    assert client.post("/v1/admin/contacts", json=body).status_code == 409


def test_patch_contact_name(client, admin_session):
    cid = client.post(
        "/v1/admin/contacts",
        json={"name": "Old", "phone_e164": "+15551230303", "timezone": _TZ},
    ).json()["id"]
    r = client.patch(f"/v1/admin/contacts/{cid}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


def test_delete_contact(client, admin_session):
    cid = client.post(
        "/v1/admin/contacts",
        json={"name": "Bye", "phone_e164": "+15551230404", "timezone": _TZ},
    ).json()["id"]
    assert client.delete(f"/v1/admin/contacts/{cid}").status_code == 204
    assert client.get(f"/v1/admin/contacts/{cid}").status_code == 404


def test_viewer_cannot_create(client, async_database_url):
    _viewer_cookie(client, async_database_url)
    r = client.post(
        "/v1/admin/contacts",
        json={"name": "X", "phone_e164": "+15551230505", "timezone": _TZ},
    )
    assert r.status_code == 403


def test_create_requires_session(bare_client):
    assert (
        bare_client.post(
            "/v1/admin/contacts",
            json={"name": "X", "phone_e164": "+15551230606", "timezone": _TZ},
        ).status_code
        == 401
    )
