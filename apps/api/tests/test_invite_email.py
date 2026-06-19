"""Invite email content + best-effort orchestration (spec 2026-06-19). Pure unit tests:
the mailer is a fake that records (or raises), so no transport runs here."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from usan_api import invite_email
from usan_api.db.base import AdminRole
from usan_api.invite_email import render_invite_email, send_invite_email

_URL = "http://app/v1/auth/accept-invite?token=SECRET-TKN"
_EXP = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)


def _settings(timeout: float = 30) -> SimpleNamespace:
    # send_invite_email reads only invite_email_timeout_s (the overall send ceiling).
    return SimpleNamespace(invite_email_timeout_s=timeout)


def test_render_includes_link_role_and_expiry_in_both_parts():
    subject, text_body, html_body = render_invite_email(
        role="admin", accept_url=_URL, expires_at=_EXP, invited_by="boss@x.com"
    )
    assert subject == "You're invited to USAN Admin"
    for part in (text_body, html_body):
        assert _URL in part
        assert "admin" in part
        assert "2026-07-01 09:30 UTC" in part
    assert "boss@x.com" in text_body  # the inviter is named


def test_render_handles_missing_inviter():
    _, text_body, html_body = render_invite_email(
        role="viewer", accept_url=_URL, expires_at=_EXP, invited_by=None
    )
    assert " by " not in text_body  # no dangling "by" when invited_by is None
    assert "viewer" in html_body


class _FakeMailer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, text_body: str, html_body: str) -> None:
        self.calls.append({"to": to, "subject": subject})
        if self.fail:
            raise RuntimeError("transport down")


def _invite() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="invitee@x.com",
        role=AdminRole.ADMIN,
        expires_at=_EXP,
        invited_by="boss@x.com",
    )


async def test_send_returns_true_and_calls_mailer():
    mailer = _FakeMailer()
    ok = await send_invite_email(_settings(), invite=_invite(), accept_url=_URL, mailer=mailer)
    assert ok is True
    assert mailer.calls == [{"to": "invitee@x.com", "subject": "You're invited to USAN Admin"}]


async def test_send_returns_false_and_never_raises_on_mailer_error():
    mailer = _FakeMailer(fail=True)
    ok = await send_invite_email(_settings(), invite=_invite(), accept_url=_URL, mailer=mailer)
    assert ok is False  # swallowed: the invite is already valid, admin falls back to copy-link


async def test_send_does_not_log_the_token(capsys):
    # The token rides inside accept_url; a failed send must not leak it to logs.
    import sys

    from loguru import logger

    sink_id = logger.add(sys.stderr, level="WARNING")
    try:
        await send_invite_email(
            _settings(), invite=_invite(), accept_url=_URL, mailer=_FakeMailer(fail=True)
        )
    finally:
        logger.remove(sink_id)
    captured = capsys.readouterr()
    assert "SECRET-TKN" not in captured.err
    assert "SECRET-TKN" not in captured.out


def test_module_default_mailer_is_gmail():
    # Sanity: the production default path constructs the keyless Gmail transport.
    assert invite_email.GmailMailer.__name__ == "GmailMailer"


async def test_send_times_out_and_returns_false():
    # A hung transport (e.g. a stuck ADC metadata refresh) must not block the request:
    # the overall send is bounded by invite_email_timeout_s and surfaced as False.
    class _HangingMailer:
        async def send(self, *, to: str, subject: str, text_body: str, html_body: str) -> None:
            await asyncio.sleep(5)  # never completes within the tiny test ceiling

    ok = await send_invite_email(
        _settings(timeout=0.05), invite=_invite(), accept_url=_URL, mailer=_HangingMailer()
    )
    assert ok is False  # the timeout unblocked the caller instead of hanging
