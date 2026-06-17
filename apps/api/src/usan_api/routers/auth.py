import contextlib
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse

from usan_api import oauth
from usan_api.admin_session import (
    SESSION_COOKIE_NAME,
    TX_COOKIE_NAME,
    clear_session_cookie,
    clear_tx_cookie,
    decode_session,
    decode_tx,
    issue_session,
    issue_tx,
    set_session_cookie,
    set_tx_cookie,
)
from usan_api.auth import AdminPrincipal, require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.schemas.auth import MeResponse
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_SSO_DISABLED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SSO not configured"
)


def _fail(status_code: int, detail: str, settings: Settings) -> JSONResponse:
    """A JSON error response that also clears the OAuth-tx cookie.

    The tx cookie holds a short-lived PKCE verifier + CSRF state; it must not outlive
    the transaction, so every callback failure path clears it (not only success).
    """
    resp = JSONResponse({"detail": detail}, status_code=status_code)
    clear_tx_cookie(resp, settings)
    return resp


@router.get("/login")
async def login(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """Begin Google OAuth: set the PKCE/state tx cookie and redirect to Google."""
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    state = oauth.new_state()
    verifier, challenge = oauth.new_pkce()
    url = oauth.build_authorization_url(settings, state=state, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    set_tx_cookie(resp, issue_tx(state, verifier, settings), settings)
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
    user = await admin_users_repo.get_admin_user(db, email)
    if user is None:
        # Per design §9, denials ARE audited (a security trail). Any Google-verified
        # identity reaching here writes one row; the /v1/auth/* rate limit bounds the
        # write rate, which is acceptable for this low-traffic admin plane.
        await admin_audit.record(
            db, actor_email=email, action="auth.denied", entity_type="admin_user", entity_id=email
        )
        await db.commit()
        logger.bind(email=email).warning("SSO login rejected: email not on allow-list")
        return _fail(status.HTTP_403_FORBIDDEN, "not authorized", settings)

    await admin_audit.record(
        db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
    )
    await db.commit()
    resp = RedirectResponse(
        settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER
    )
    # B1: mint an identity-only session (no active org yet). Task B3 resolves the
    # active org from the caller's memberships and re-issues with role/active_org set.
    set_session_cookie(
        resp,
        issue_session(
            email,
            active_org_id=None,
            role=None,
            is_super_admin=user.is_super_admin,
            acting_as=False,
            settings=settings,
        ),
        settings,
    )
    clear_tx_cookie(resp, settings)
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
            logger.bind(email=email).info("admin logout")
    return resp


@router.get("/me", response_model=MeResponse)
async def me(principal: AdminPrincipal = Depends(require_admin_session)) -> MeResponse:
    # B1: role rides the session claims (None when no org is active). Task B3 extends
    # this to the full org-aware payload (orgs list, active_org, super/act-as flags).
    return MeResponse(email=principal.email, role=principal.role.value if principal.role else "")
