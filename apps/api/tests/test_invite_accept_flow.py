"""Task C2: invite-aware OAuth accept flow.

The accept-invite endpoint stashes the invite token in a short-lived cookie and
bounces through Google OAuth; the callback consumes the invite on return. The exact
Google-verified-email == invite.email match is the security gate — on any failure
(mismatch / expired / revoked / unknown) the invite is NOT consumed and NO session is
issued. A valid pending unexpired matching invite bypasses the bootstrap allow-list,
creating identity + membership for a brand-new invitee.

OAuth is mocked exactly as in tests/test_auth_flow_p2.py (monkeypatching
usan_api.oauth.exchange_code / verify_id_token); the `set_verified` fixture lets each
test choose the Google-verified email the callback will see. `seed_invite`,
`invite_status`, and `membership_exists` read/write the global (non-RLS) tables via
the superuser engine.
"""

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import oauth
from usan_api.admin_session import SESSION_COOKIE_NAME, decode_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


# ---------------------------------------------------------------------------
# OAuth mock: a mutable holder the monkeypatched verify_id_token reads, so each
# test selects which Google-verified email comes back from the round-trip.
# ---------------------------------------------------------------------------
@pytest.fixture
def set_verified(monkeypatch):
    holder = {"email": "unset@x.com"}

    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": holder["email"], "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)

    def _set(email: str) -> None:
        holder["email"] = email

    return _set


# ---------------------------------------------------------------------------
# seeding / readback helpers (superuser engine; invitations is non-RLS)
# ---------------------------------------------------------------------------
async def seed_invite(
    db_url: str,
    org_id: uuid.UUID,
    email: str,
    role: AdminRole,
    *,
    status: str = "pending",
    expires_at: datetime | None = None,
) -> str:
    """Insert a pending invite row directly and return its token."""
    token = secrets.token_urlsafe(32)
    exp = expires_at if expires_at is not None else datetime.now(UTC) + timedelta(hours=168)
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO invitations "
                    "(organization_id, email, role, token, status, invited_by, expires_at) "
                    "VALUES (:o, :e, CAST(:r AS admin_role), :t, "
                    "CAST(:s AS invite_status), 'boss@x.com', :x)"
                ),
                {
                    "o": org_id,
                    "e": email.lower(),
                    "r": role.value,
                    "t": token,
                    "s": status,
                    "x": exp,
                },
            )
    finally:
        await engine.dispose()
    return token


async def invite_status(db_url: str, token: str) -> str | None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text("SELECT status FROM invitations WHERE token = :t"), {"t": token}
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()


async def _cleanup(db_url: str, *org_ids: uuid.UUID, identities: tuple[str, ...] = ()) -> None:
    """Drop org-FK-ing rows the accept flow created before ``two_orgs`` teardown.

    The flow writes admin_audit_log rows (invite.accept / invite.accept_denied /
    auth.login) into the target org, and a successful accept points the brand-new
    identity's ``admin_users.last_active_org_id`` at it. Both FK the org
    (fk_admin_audit_log_organization, fk_admin_users_last_org), so they must go before
    ``two_orgs`` deletes the org (mirrors test_rls_p2_isolation._delete_audit_in_orgs).
    """
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM admin_audit_log WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
            if identities:
                emails = [e.lower() for e in identities]
                # Membership FKs the identity; drop it before the admin_users row.
                await conn.execute(
                    text("DELETE FROM memberships WHERE email = ANY(:emails)"),
                    {"emails": emails},
                )
                await conn.execute(
                    text("DELETE FROM admin_users WHERE email = ANY(:emails)"),
                    {"emails": emails},
                )
    finally:
        await engine.dispose()


async def membership_exists(db_url: str, email: str, org_id: uuid.UUID) -> bool:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM memberships WHERE email = :e AND organization_id = :o"),
                    {"e": email.lower(), "o": org_id},
                )
            ).scalar_one_or_none()
            return row is not None
    finally:
        await engine.dispose()


async def _seed_member(db_url: str, email: str, org_id: uuid.UUID, role: AdminRole) -> None:
    """Seed an active identity + org membership (superuser engine; non-RLS tables)."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, false, 'active', 'test') ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "VALUES (:e, :o, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "o": org_id, "r": role.value},
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# accept-invite -> Google bounce -> callback, carrying cookies across.
# ---------------------------------------------------------------------------
def _accept(sso_client, *, token, verified_email, set_verified):
    set_verified(verified_email)
    r = sso_client.get(f"/v1/auth/accept-invite?token={token}", follow_redirects=False)
    assert r.status_code == 302, r.text
    # The real CSRF state lives in the Google authorization URL the endpoint built;
    # the matching tx cookie was set on the client, so feed the same state back.
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    return sso_client.get(f"/v1/auth/callback?code=abc&state={state}", follow_redirects=False)


def test_accept_brand_new_invitee(sso_client, async_database_url, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(seed_invite(async_database_url, org_a, "newbie@x.com", AdminRole.VIEWER))
    resp = _accept(
        sso_client, token=token, verified_email="newbie@x.com", set_verified=set_verified
    )
    assert resp.status_code == 303  # success -> post-login redirect
    session = resp.cookies.get(SESSION_COOKIE_NAME)
    assert session is not None
    claims = decode_session(session, get_settings())
    assert claims["sub"] == "newbie@x.com"
    assert claims["active_org"] == str(org_a)
    assert claims["role"] == "viewer"
    assert claims["acting_as"] is False
    assert asyncio.run(membership_exists(async_database_url, "newbie@x.com", org_a)) is True
    assert asyncio.run(invite_status(async_database_url, token)) == "accepted"
    asyncio.run(_cleanup(async_database_url, org_a, identities=("newbie@x.com",)))


def test_accept_email_mismatch_does_not_consume(
    sso_client, async_database_url, two_orgs, set_verified
):
    org_a, _ = two_orgs
    token = asyncio.run(seed_invite(async_database_url, org_a, "intended@x.com", AdminRole.ADMIN))
    resp = _accept(
        sso_client, token=token, verified_email="someone-else@x.com", set_verified=set_verified
    )
    assert resp.status_code == 303
    assert "status=error&reason=mismatch" in resp.headers["location"]
    assert resp.cookies.get(SESSION_COOKIE_NAME) is None
    assert asyncio.run(invite_status(async_database_url, token)) == "pending"  # NOT consumed
    assert asyncio.run(membership_exists(async_database_url, "someone-else@x.com", org_a)) is False
    asyncio.run(_cleanup(async_database_url, org_a))


def test_accept_expired_invite(sso_client, async_database_url, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(
        seed_invite(
            async_database_url,
            org_a,
            "late@x.com",
            AdminRole.VIEWER,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    resp = _accept(sso_client, token=token, verified_email="late@x.com", set_verified=set_verified)
    assert "status=error&reason=expired" in resp.headers["location"]
    assert asyncio.run(invite_status(async_database_url, token)) == "pending"
    asyncio.run(_cleanup(async_database_url, org_a))


def test_accept_revoked_invite(sso_client, async_database_url, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(
        seed_invite(async_database_url, org_a, "rev@x.com", AdminRole.VIEWER, status="revoked")
    )
    resp = _accept(sso_client, token=token, verified_email="rev@x.com", set_verified=set_verified)
    assert "status=error&reason=revoked" in resp.headers["location"]
    asyncio.run(_cleanup(async_database_url, org_a))


def test_accept_already_member_uses_live_role(
    sso_client, async_database_url, two_orgs, set_verified
):
    # Already a VIEWER member; a later invite grants ADMIN. The idempotent re-accept must
    # issue a session at the LIVE membership role (viewer), NOT the invite's admin role,
    # and consume the still-pending invite. Exercises the already-member branch + audit.
    org_a, _ = two_orgs
    asyncio.run(_seed_member(async_database_url, "member@x.com", org_a, AdminRole.VIEWER))
    token = asyncio.run(seed_invite(async_database_url, org_a, "member@x.com", AdminRole.ADMIN))
    resp = _accept(
        sso_client, token=token, verified_email="member@x.com", set_verified=set_verified
    )
    assert resp.status_code == 303
    session = resp.cookies.get(SESSION_COOKIE_NAME)
    assert session is not None
    claims = decode_session(session, get_settings())
    assert claims["active_org"] == str(org_a)
    assert claims["role"] == "viewer"  # live membership role, not the invite's admin
    assert asyncio.run(invite_status(async_database_url, token)) == "accepted"
    asyncio.run(_cleanup(async_database_url, org_a, identities=("member@x.com",)))


def test_accept_missing_token_400(sso_client):
    assert sso_client.get("/v1/auth/accept-invite", follow_redirects=False).status_code == 400
