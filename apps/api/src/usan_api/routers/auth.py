import contextlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse

from usan_api import oauth
from usan_api.admin_session import (
    INVITE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    TX_COOKIE_NAME,
    clear_invite_cookie,
    clear_session_cookie,
    clear_tx_cookie,
    decode_invite,
    decode_session,
    decode_tx,
    issue_invite,
    issue_session,
    issue_tx,
    set_invite_cookie,
    set_session_cookie,
    set_tx_cookie,
)
from usan_api.auth import AdminPrincipal, require_admin_session
from usan_api.db.base import AdminRole, InviteStatus
from usan_api.db.models import Invitation
from usan_api.db.session import get_db
from usan_api.invites import build_accept_error_url
from usan_api.masking import email_fingerprint
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.repositories import invitations as invitations_repo
from usan_api.repositories import memberships as memberships_repo
from usan_api.repositories import organizations as organizations_repo
from usan_api.schemas.auth import MeResponse, OrgSummary, SwitchOrgRequest
from usan_api.settings import Settings, get_settings
from usan_api.tenant_context import set_tenant_context

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_SSO_DISABLED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SSO not configured"
)


def _fail(status_code: int, detail: str, settings: Settings) -> JSONResponse:
    """A JSON error response that clears the OAuth-tx AND invite cookies.

    The tx cookie holds a short-lived PKCE verifier + CSRF state; the invite cookie holds
    a pending invite token. Neither must outlive the transaction, so every callback
    failure path clears both (not only success) — otherwise a failed invite-accept would
    leave the invite cookie alive for its full TTL and replay on the next callback.
    """
    resp = JSONResponse({"detail": detail}, status_code=status_code)
    clear_tx_cookie(resp, settings)
    clear_invite_cookie(resp, settings)
    return resp


@router.get("/login")
async def login(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """Begin Google OAuth: set the PKCE/state tx cookie and redirect to Google.

    Also clears any stale invite cookie: an abandoned /accept-invite bounce leaves the
    invite cookie alive for its full TTL, and since both cookies are sent to /callback a
    leftover invite would route this *normal* login through the invite-accept branch (a
    one-shot 'issued to a different email' failure). The email-match gate stays the real
    boundary, but clearing here — symmetric with /callback's failure paths — removes the
    hijack window entirely.
    """
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    state = oauth.new_state()
    verifier, challenge = oauth.new_pkce()
    url = oauth.build_authorization_url(settings, state=state, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    set_tx_cookie(resp, issue_tx(state, verifier, settings), settings)
    clear_invite_cookie(resp, settings)
    return resp


@router.get("/accept-invite")
async def accept_invite(
    token: str | None = None,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Begin invite acceptance: stash the invite token in a short-lived cookie and
    bounce through Google OAuth. The callback consumes the invite on return."""
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    if not token or not token.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing invite token")
    state = oauth.new_state()
    verifier, challenge = oauth.new_pkce()
    url = oauth.build_authorization_url(settings, state=state, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    set_tx_cookie(resp, issue_tx(state, verifier, settings), settings)
    set_invite_cookie(resp, issue_invite(token, settings), settings)
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Complete Google OAuth: verify, allow-list check, issue the session cookie.

    Every failure path returns via _fail, which clears the OAuth-tx cookie so the
    short-lived PKCE verifier + CSRF state never outlive the transaction.
    """
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    if error:
        return _fail(status.HTTP_400_BAD_REQUEST, "oauth error", settings)
    tx_cookie = request.cookies.get(TX_COOKIE_NAME)
    if not code or not state or not tx_cookie:
        return _fail(status.HTTP_400_BAD_REQUEST, "missing oauth params", settings)
    try:
        tx = decode_tx(tx_cookie, settings)
    except Exception:  # noqa: BLE001 - any bad/tampered tx cookie is a 400
        return _fail(status.HTTP_400_BAD_REQUEST, "invalid oauth transaction", settings)
    if not secrets.compare_digest(str(tx["state"]), state):
        return _fail(status.HTTP_400_BAD_REQUEST, "state mismatch", settings)

    try:
        id_token = await oauth.exchange_code(settings, code=code, code_verifier=str(tx["cv"]))
        claims: dict[str, Any] = oauth.verify_id_token(settings, id_token)
    except oauth.OAuthError:
        return _fail(status.HTTP_403_FORBIDDEN, "authentication failed", settings)

    email = str(claims.get("email", "")).lower()
    if not email:
        return _fail(status.HTTP_403_FORBIDDEN, "no email in token", settings)

    # Invite-accept bounce: a valid pending invite for this Google-verified email is
    # the authorization, so it runs BEFORE (and bypasses) the allow-list check below.
    invite_cookie = request.cookies.get(INVITE_COOKIE_NAME)
    if invite_cookie:
        return await _complete_invite_accept(db, settings, email=email, invite_cookie=invite_cookie)

    user = await admin_users_repo.get_admin_user(db, email)
    if user is None or user.status != "active":
        # Per design §9, denials ARE audited (a security trail). Any Google-verified
        # identity reaching here writes one row; the /v1/auth/* rate limit bounds the
        # write rate, which is acceptable for this low-traffic admin plane.
        await admin_audit.record(
            db, actor_email=email, action="auth.denied", entity_type="admin_user", entity_id=email
        )
        await db.commit()
        logger.bind(email_fp=email_fingerprint(email)).warning(
            "SSO login rejected: not allow-listed/active"
        )
        return _fail(status.HTTP_403_FORBIDDEN, "not authorized", settings)

    # B3: resolve the active org from the caller's memberships. A 0-membership non-
    # super-admin has nowhere to land and is denied; a super-admin with no memberships
    # logs in with no active org (they pick one / act-as via the org console).
    memberships = await memberships_repo.list_memberships_for_email(db, email)
    if not memberships and not user.is_super_admin:
        await admin_audit.record(
            db, actor_email=email, action="auth.denied", entity_type="admin_user", entity_id=email
        )
        await db.commit()
        logger.bind(email_fp=email_fingerprint(email)).warning(
            "SSO login rejected: no org membership"
        )
        return _fail(status.HTTP_403_FORBIDDEN, "not authorized", settings)

    active_org_id = None
    role: AdminRole | None = None
    if memberships:
        # Prefer the org the person last used; otherwise the first membership.
        chosen = next(
            (m for m in memberships if m.organization_id == user.last_active_org_id),
            memberships[0],
        )
        active_org_id, role = chosen.organization_id, chosen.role
        await admin_users_repo.set_last_active_org(db, email=email, org_id=active_org_id)

    await admin_audit.record(
        db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
    )
    await db.commit()
    resp = RedirectResponse(
        settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(
        resp,
        issue_session(
            email,
            active_org_id=active_org_id,
            role=role,
            is_super_admin=user.is_super_admin,
            acting_as=False,
            settings=settings,
        ),
        settings,
    )
    clear_tx_cookie(resp, settings)
    return resp


async def _complete_invite_accept(
    db: AsyncSession,
    settings: Settings,
    *,
    email: str,
    invite_cookie: str,
) -> Response:
    """Consume a pending invite for the Google-verified email and issue a session.

    A valid pending invite is the authorization for a brand-new (non-allow-listed)
    person — the deliberate allow-list bypass. The exact email match against the
    Google-verified identity is the security gate; the token is not a secret.
    """

    def _err(reason: str) -> Response:
        resp = RedirectResponse(
            build_accept_error_url(settings, reason), status_code=status.HTTP_303_SEE_OTHER
        )
        clear_tx_cookie(resp, settings)
        clear_invite_cookie(resp, settings)
        return resp

    async def _audit_denied(invite: Invitation, reason: str) -> None:
        await set_tenant_context(db, invite.organization_id)
        await admin_audit.record(
            db,
            actor_email=email,
            action="invite.accept_denied",
            entity_type="invitation",
            entity_id=invite.email,
            detail={"invite_email": invite.email, "attempted_email": email, "reason": reason},
        )
        await db.commit()

    try:
        claims = decode_invite(invite_cookie, settings)
        token = str(claims["invite_token"])
    except Exception:  # noqa: BLE001 - any bad/tampered invite cookie is "invalid"
        return _err("invalid")

    # Row-lock the invite for the whole consume: serializes two concurrent accepts of the
    # same token so it is marked accepted exactly once (no duplicate audit/membership work).
    invite = await invitations_repo.get_by_token(db, token, for_update=True)
    if invite is None:
        logger.warning("invite accept: unknown token")
        return _err("invalid")

    # Email is checked first and unconditionally — never leak invite state to a
    # mismatched identity.
    if invite.email != email:
        await _audit_denied(invite, "mismatch")
        return _err("mismatch")

    # Idempotent re-click: already a member of the target org -> success, no re-consume.
    # Issue the session at the LIVE membership role, not invite.role — they differ if the
    # member's role was changed after this invite was created.
    existing_membership = await memberships_repo.get_membership(db, email, invite.organization_id)
    if existing_membership is not None:
        # Still leave a trail for the org entry (audit log is RLS-scoped to the org).
        await set_tenant_context(db, invite.organization_id)
        if invite.status is InviteStatus.PENDING:
            await invitations_repo.mark_accepted(db, invite)
            await admin_audit.record(
                db,
                actor_email=email,
                action="invite.accept",
                entity_type="invitation",
                entity_id=invite.email,
            )
        await admin_audit.record(
            db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
        )
        await db.commit()
        user = await admin_users_repo.get_admin_user(db, email)
        return _issue_invite_session(
            settings,
            email=email,
            org_id=invite.organization_id,
            role=existing_membership.role,
            is_super_admin=bool(user and user.is_super_admin),
        )

    now = datetime.now(UTC)
    if invite.status is not InviteStatus.PENDING:
        await _audit_denied(invite, invite.status.value)
        return _err("revoked" if invite.status is InviteStatus.REVOKED else "invalid")
    if invite.expires_at <= now:
        await _audit_denied(invite, "expired")
        return _err("expired")

    # Valid -> consume. add_member ensures the identity (FK target) exists, so a
    # brand-new invitee gets their admin_users row created here.
    await set_tenant_context(db, invite.organization_id)
    user = await admin_users_repo.ensure_identity(db, email=email)
    await memberships_repo.add_member(
        db,
        email=email,
        org_id=invite.organization_id,
        role=invite.role,
        added_by=f"invite:{invite.invited_by}",
    )
    await invitations_repo.mark_accepted(db, invite)
    await admin_audit.record(
        db,
        actor_email=email,
        action="invite.accept",
        entity_type="invitation",
        entity_id=invite.email,
    )
    await admin_audit.record(
        db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
    )
    await admin_users_repo.set_last_active_org(db, email=email, org_id=invite.organization_id)
    await db.commit()
    return _issue_invite_session(
        settings,
        email=email,
        org_id=invite.organization_id,
        role=invite.role,
        is_super_admin=user.is_super_admin,
    )


def _issue_invite_session(
    settings: Settings,
    *,
    email: str,
    org_id: uuid.UUID,
    role: AdminRole,
    is_super_admin: bool,
) -> Response:
    resp = RedirectResponse(
        settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(
        resp,
        issue_session(
            email,
            active_org_id=org_id,
            role=role,
            is_super_admin=is_super_admin,
            acting_as=False,
            settings=settings,
        ),
        settings,
    )
    clear_tx_cookie(resp, settings)
    clear_invite_cookie(resp, settings)
    return resp


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Clear the session cookie. Idempotent; safe without an active session."""
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(resp, settings)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        # Best-effort attribution + audit (design §9 lists auth.logout); logout must
        # never fail on a bad/expired cookie or an audit-write hiccup.
        with contextlib.suppress(Exception):
            email = str(decode_session(cookie, settings)["sub"]).lower()
            await admin_audit.record(
                db,
                actor_email=email,
                action="auth.logout",
                entity_type="admin_user",
                entity_id=email,
            )
            await db.commit()
            logger.bind(email_fp=email_fingerprint(email)).info("admin logout")
    return resp


@router.get("/me", response_model=MeResponse)
async def me(
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """The caller's identity: their memberships (orgs + role), the active org, and the
    super/act-as flags. Super-admin org browsing for act-as is served separately by
    Unit C's /v1/admin/organizations (not /me)."""
    memberships = await memberships_repo.list_memberships_for_email(db, principal.email)
    role_by_org = {m.organization_id: m.role.value for m in memberships}
    # Batch-load every org we need (memberships + the active org) in one query,
    # then build the response from the in-memory dict — no per-row DB round-trips.
    needed_ids = [m.organization_id for m in memberships]
    if principal.active_org_id is not None:
        needed_ids.append(principal.active_org_id)
    orgs_by_id = await organizations_repo.get_orgs_by_ids(db, needed_ids)
    summaries: list[OrgSummary] = []
    for m in memberships:
        o = orgs_by_id.get(m.organization_id)
        if o:
            summaries.append(OrgSummary(id=o.id, name=o.name, slug=o.slug, role=m.role.value))
    active = None
    if principal.active_org_id is not None:
        o = orgs_by_id.get(principal.active_org_id)
        if o:
            # An act-as super-admin has no membership row in the active org -> role None.
            active = OrgSummary(id=o.id, name=o.name, slug=o.slug, role=role_by_org.get(o.id))
    return MeResponse(
        email=principal.email,
        is_super_admin=principal.is_super_admin,
        acting_as=principal.acting_as,
        active_org=active,
        orgs=summaries,
    )


@router.post("/switch-org", response_model=MeResponse)
async def switch_org(
    body: SwitchOrgRequest,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Re-issue the session scoped to a different org.

    A member switches to one of their orgs at their membership role. A super-admin
    switching to a non-member org *acts-as* it (full ADMIN write, audited in the
    target org). A non-super-admin without a membership in the target is denied.
    The new active org is remembered as the login default.
    """
    org = await organizations_repo.get_org(db, body.organization_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    m = await memberships_repo.get_membership(db, principal.email, org.id)
    if m is not None:
        role, acting_as, action = m.role, False, "auth.switch_org"
    elif principal.is_super_admin:
        role, acting_as, action = AdminRole.ADMIN, True, "auth.act_as"
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this organization")
    # Every org-context change is audited in the TARGET org (admin_audit_log is
    # RLS-scoped) with the real operator's email — a member switch and a super-admin
    # act-as both gate all subsequent data access, so both leave a trail (design §9).
    await set_tenant_context(db, org.id)
    await admin_audit.record(
        db,
        actor_email=principal.email,
        action=action,
        entity_type="organization",
        entity_id=str(org.id),
    )
    await admin_users_repo.set_last_active_org(db, email=principal.email, org_id=org.id)
    await db.commit()
    out = MeResponse(
        email=principal.email,
        is_super_admin=principal.is_super_admin,
        acting_as=acting_as,
        active_org=OrgSummary(
            id=org.id,
            name=org.name,
            slug=org.slug,
            role=role.value if not acting_as else None,
        ),
        orgs=[],  # client refetches /me for the full list
    )
    resp = JSONResponse(out.model_dump(mode="json"))
    set_session_cookie(
        resp,
        issue_session(
            principal.email,
            active_org_id=org.id,
            role=role,
            is_super_admin=principal.is_super_admin,
            acting_as=acting_as,
            settings=settings,
        ),
        settings,
    )
    return resp
