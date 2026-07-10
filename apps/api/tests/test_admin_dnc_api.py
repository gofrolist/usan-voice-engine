import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


def test_add_list_remove_dnc(client, admin_session):
    phone = "+15551240001"
    assert (
        client.post("/v1/admin/dnc", json={"phone_e164": phone, "reason": "req"}).status_code == 201
    )
    listed = client.get("/v1/admin/dnc").json()
    assert any(row["masked_phone"].endswith("0001") for row in listed)
    assert all("phone_e164" not in row for row in listed)  # masked only
    assert client.delete(f"/v1/admin/dnc/{phone}").status_code == 204


def test_remove_missing_404(client, admin_session):
    assert client.delete("/v1/admin/dnc/+15559990000").status_code == 404


def test_viewer_can_list_cannot_add(client, async_database_url):
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-dnc@example.com", "viewer"))
    token = issue_session(
        "viewer-dnc@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/v1/admin/dnc").status_code == 200
    assert client.post("/v1/admin/dnc", json={"phone_e164": "+15551240002"}).status_code == 403


def test_dnc_requires_session(bare_client):
    assert bare_client.get("/v1/admin/dnc").status_code == 401
