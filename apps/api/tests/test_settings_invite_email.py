import pytest
from pydantic import ValidationError

from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def test_invite_email_defaults_are_inert():
    # Ship-inert (spec 2026-06-19): default OFF, so invites stay copy-link-only until
    # a deploy flips the flag. The sender default is a real-looking mailbox so a
    # flag-on-without-sender misconfig is the only way to trip the validator.
    s = Settings(**_BASE)
    assert s.invite_email_enabled is False
    assert s.invite_email_sender == "noreply@usanretirement.com"
    assert s.invite_email_from_name == "USAN Admin"
    assert s.invite_email_timeout_s == 10


def test_invite_email_enabled_with_sender_accepted():
    s = Settings(**_BASE, INVITE_EMAIL_ENABLED="true", INVITE_EMAIL_SENDER="invites@usan.com")
    assert s.invite_email_enabled is True
    assert s.invite_email_sender == "invites@usan.com"


def test_invite_email_enabled_blank_sender_rejected():
    # The sender is the domain-wide-delegation subject; a blank one would 400 on every
    # Gmail send — fail at startup, not on the first admin click.
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_BASE, INVITE_EMAIL_ENABLED="true", INVITE_EMAIL_SENDER="   ")
    msg = str(exc_info.value)
    assert "INVITE_EMAIL_SENDER" in msg


@pytest.mark.parametrize("bad", ["0", "61"])
def test_invite_email_timeout_bounds_enforced(bad: str):
    with pytest.raises(ValidationError):
        Settings(**_BASE, INVITE_EMAIL_TIMEOUT_S=bad)
