import asyncio
import uuid

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


def _member_cookie(email, org_id, role):
    token = issue_session(
        email,
        active_org_id=org_id,
        role=role,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def test_create_invite_returns_accept_url(client, admin_session):
    r = client.post("/v1/admin/invites", json={"email": "new@x.com", "role": "viewer"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new@x.com"
    assert body["role"] == "viewer"
    assert body["status"] == "pending"
    assert "/v1/auth/accept-invite?token=" in body["accept_url"]


def test_create_invite_idempotent_reinvite(client, admin_session):
    a = client.post("/v1/admin/invites", json={"email": "dup@x.com", "role": "admin"}).json()
    b = client.post("/v1/admin/invites", json={"email": "dup@x.com", "role": "viewer"}).json()
    assert a["id"] == b["id"]  # same row regenerated
    listed = client.get("/v1/admin/invites").json()
    assert [i["email"] for i in listed] == ["dup@x.com"]  # exactly one pending


def test_create_invite_rejects_existing_member(client, admin_session):
    # admin@example.com is already a member (the admin_session fixture seeds them).
    r = client.post("/v1/admin/invites", json={"email": "admin@example.com", "role": "viewer"})
    assert r.status_code == 409


def test_viewer_cannot_manage_invites(client, async_database_url):
    from tests.conftest import _seed_admin_user_async  # seeds identity + usan membership

    org_id = asyncio.run(_seed_admin_user_async(async_database_url, "view@example.com", "viewer"))
    cookie = _member_cookie("view@example.com", org_id, AdminRole.VIEWER)
    # ADMIN-only on every endpoint, including list.
    assert client.get("/v1/admin/invites", cookies=cookie).status_code == 403
    r = client.post("/v1/admin/invites", json={"email": "x@x.com"}, cookies=cookie)
    assert r.status_code == 403


def test_revoke_and_resend(client, admin_session):
    created = client.post("/v1/admin/invites", json={"email": "r@x.com", "role": "viewer"}).json()
    iid = created["id"]
    resent = client.post(f"/v1/admin/invites/{iid}/resend")
    assert resent.status_code == 200
    assert "/v1/auth/accept-invite?token=" in resent.json()["accept_url"]
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 204
    # revoking again -> 409 (not pending)
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 409
    assert client.get("/v1/admin/invites").json() == []  # no longer pending


def test_revoke_unknown_invite_404(client, admin_session):
    assert client.delete(f"/v1/admin/invites/{uuid.uuid4()}").status_code == 404
