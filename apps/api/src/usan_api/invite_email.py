"""Invite email content + best-effort send orchestration (spec 2026-06-19).

``render_invite_email`` builds the subject + text/HTML bodies from an invitation; the
HTML mirrors the text and carries the accept link as a button plus a visible fallback
URL. ``send_invite_email`` wraps the transport in a best-effort try/except: the invite is
already committed and the link works regardless, so a send failure is logged (recipient +
invite id + error type only — never the token, which rides inside the accept link) and
reported as ``False`` rather than raised.
"""

import asyncio
import html as html_lib
from datetime import datetime

from loguru import logger

from usan_api.db.models import Invitation
from usan_api.gmail_sender import GmailMailer, Mailer
from usan_api.settings import Settings

_SUBJECT = "You're invited to USAN Admin"


def _format_expiry(expires_at: datetime) -> str:
    # Portable, unambiguous, UTC-explicit (avoids %-d / locale-specific formats).
    return expires_at.strftime("%Y-%m-%d %H:%M UTC")


def render_invite_email(
    *, role: str, accept_url: str, expires_at: datetime, invited_by: str | None
) -> tuple[str, str, str]:
    """Return ``(subject, text_body, html_body)`` for an invitation email."""
    inviter = f" by {invited_by}" if invited_by else ""
    expiry = _format_expiry(expires_at)

    text_body = (
        f"You've been invited to the USAN Admin console as {role}{inviter}.\n\n"
        f"Accept your invitation:\n{accept_url}\n\n"
        f"This invitation expires on {expiry}.\n\n"
        "If you weren't expecting this, you can safely ignore this email."
    )

    # Escape every interpolated value: invited_by is an actor-supplied email and
    # accept_url is reflected into an href, so treat both as untrusted in HTML context.
    esc_url = html_lib.escape(accept_url, quote=True)
    esc_role = html_lib.escape(role)
    esc_inviter = f" by {html_lib.escape(invited_by)}" if invited_by else ""
    esc_expiry = html_lib.escape(expiry)
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;color:#1f2933;line-height:1.5">'
        f"<p>You've been invited to the <strong>USAN Admin</strong> console "
        f"as {esc_role}{esc_inviter}.</p>"
        f'<p><a href="{esc_url}" style="display:inline-block;padding:10px 18px;'
        'background:#1f2933;color:#ffffff;text-decoration:none;border-radius:8px">'
        "Accept invitation</a></p>"
        "<p>Or paste this link into your browser:<br>"
        f'<a href="{esc_url}">{esc_url}</a></p>'
        f'<p style="color:#6b7280">This invitation expires on {esc_expiry}.</p>'
        '<p style="color:#6b7280">If you weren\'t expecting this, you can safely '
        "ignore this email.</p>"
        "</div>"
    )
    return _SUBJECT, text_body, html_body


async def send_invite_email(
    settings: Settings, *, invite: Invitation, accept_url: str, mailer: Mailer | None = None
) -> bool:
    """Best-effort: email the accept link to the invitee. Return True if sent.

    Never raises — the invite is already persisted and its link works regardless, so any
    transport failure is logged and reported as ``False`` so the caller (and the admin UI)
    can fall back to copy-the-link. The token is never logged (it lives inside accept_url).
    """
    mailer = mailer if mailer is not None else GmailMailer(settings)
    subject, text_body, html_body = render_invite_email(
        role=invite.role.value,
        accept_url=accept_url,
        expires_at=invite.expires_at,
        invited_by=invite.invited_by,
    )
    try:
        # Bound the WHOLE send — including the ADC metadata-server refresh inside
        # GmailMailer (asyncio.to_thread, which the per-call httpx timeout does NOT cover).
        # Without this ceiling a hung metadata server would block the admin's request
        # indefinitely; asyncio.TimeoutError is caught below and surfaced as email_sent=False.
        await asyncio.wait_for(
            mailer.send(to=invite.email, subject=subject, text_body=text_body, html_body=html_body),
            timeout=settings.invite_email_timeout_s,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort: a send failure must not lose the invite
        logger.bind(invite_id=str(invite.id), recipient=invite.email).warning(
            "invite email send failed (invite {invite_id}, {error_type}); "
            "the link still works — admin can copy it",
            invite_id=str(invite.id),
            error_type=type(exc).__name__,
        )
        return False
