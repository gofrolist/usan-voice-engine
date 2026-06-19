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
    # Full, well-formed absolute URL (a malformed "://..." origin would still satisfy a
    # bare substring check — see the _origin guard).
    assert body["accept_url"].startswith("http://testserver/v1/auth/accept-invite?token=")


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


def test_create_invite_integrity_error_returns_409(client, admin_session, monkeypatch):
    # A concurrent create racing past the existing-pending SELECT trips the partial unique
    # index on INSERT; the router must surface a clean 409, not an opaque 500.
    from sqlalchemy.exc import IntegrityError

    from usan_api.routers import admin_invites

    async def _raise(*args, **kwargs):
        raise IntegrityError("INSERT INTO invitations", {}, Exception("duplicate key"))

    monkeypatch.setattr(admin_invites.repo, "create_invite", _raise)
    r = client.post("/v1/admin/invites", json={"email": "race@x.com", "role": "viewer"})
    assert r.status_code == 409, r.text


def test_viewer_cannot_manage_invites(client, async_database_url):
    from tests.conftest import _seed_admin_user_async  # seeds identity + usan membership

    org_id = asyncio.run(_seed_admin_user_async(async_database_url, "view@example.com", "viewer"))
    cookie = _member_cookie("view@example.com", org_id, AdminRole.VIEWER)
    # ADMIN-only on EVERY endpoint (the role dependency runs before the handler, so an
    # arbitrary id is fine — a VIEWER never reaches the 404 lookup).
    rid = uuid.uuid4()
    assert client.get("/v1/admin/invites", cookies=cookie).status_code == 403
    created = client.post("/v1/admin/invites", json={"email": "x@x.com"}, cookies=cookie)
    assert created.status_code == 403
    assert client.delete(f"/v1/admin/invites/{rid}", cookies=cookie).status_code == 403
    assert client.post(f"/v1/admin/invites/{rid}/resend", cookies=cookie).status_code == 403


def test_revoke_and_resend(client, admin_session):
    created = client.post("/v1/admin/invites", json={"email": "r@x.com", "role": "viewer"}).json()
    iid = created["id"]
    resent = client.post(f"/v1/admin/invites/{iid}/resend")
    assert resent.status_code == 200
    assert resent.json()["accept_url"].startswith("http://testserver/v1/auth/accept-invite?token=")
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 204
    # revoking again -> 409 (not pending)
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 409
    assert client.get("/v1/admin/invites").json() == []  # no longer pending


def test_revoke_unknown_invite_404(client, admin_session):
    assert client.delete(f"/v1/admin/invites/{uuid.uuid4()}").status_code == 404


# --- Invite email delivery (spec 2026-06-19). The Gmail transport is replaced with a
# recording fake at the orchestration seam, mirroring the repo.create_invite monkeypatch
# above — no live Google calls. The flag is flipped per-test via the env + cache clear.


def _enable_invite_email(monkeypatch):
    monkeypatch.setenv("INVITE_EMAIL_ENABLED", "true")
    get_settings.cache_clear()


def _fake_send(result, calls):
    async def _send(settings, *, invite, accept_url, mailer=None):
        calls.append(accept_url)
        return result

    return _send


def test_create_invite_emails_when_enabled(client, admin_session, monkeypatch):
    from usan_api import invite_email

    _enable_invite_email(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(invite_email, "send_invite_email", _fake_send(True, calls))

    r = client.post("/v1/admin/invites", json={"email": "mailme@x.com", "role": "admin"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email_sent"] is True
    # The email was sent for THIS invite's accept link.
    assert calls == [body["accept_url"]]


def test_create_invite_email_failure_still_returns_201(client, admin_session, monkeypatch):
    from usan_api import invite_email

    _enable_invite_email(monkeypatch)
    monkeypatch.setattr(invite_email, "send_invite_email", _fake_send(False, []))

    r = client.post("/v1/admin/invites", json={"email": "fail@x.com", "role": "viewer"})
    # The invite is committed BEFORE the send — a delivery failure never loses it.
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email_sent"] is False
    assert body["accept_url"].startswith("http://testserver/v1/auth/accept-invite?token=")


def test_create_invite_does_not_email_when_disabled(client, admin_session, monkeypatch):
    from usan_api import invite_email

    # Default (flag off): no attempt, email_sent is null, the seam is never called.
    called: list[str] = []
    monkeypatch.setattr(invite_email, "send_invite_email", _fake_send(True, called))

    r = client.post("/v1/admin/invites", json={"email": "quiet@x.com", "role": "admin"})
    assert r.status_code == 201, r.text
    assert r.json()["email_sent"] is None
    assert called == []  # send_invite_email was never invoked


def test_resend_invite_emails_when_enabled(client, admin_session, monkeypatch):
    from usan_api import invite_email

    created = client.post("/v1/admin/invites", json={"email": "rs@x.com", "role": "viewer"}).json()
    _enable_invite_email(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(invite_email, "send_invite_email", _fake_send(True, calls))

    r = client.post(f"/v1/admin/invites/{created['id']}/resend")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_sent"] is True
    assert calls == [body["accept_url"]]  # the freshly-rotated link was emailed


def test_list_invites_has_null_email_sent(client, admin_session):
    client.post("/v1/admin/invites", json={"email": "listed@x.com", "role": "admin"})
    listed = client.get("/v1/admin/invites").json()
    # A list read never attempts a send.
    assert all(i["email_sent"] is None for i in listed)
