"""Keyless Gmail send (spec 2026-06-19). HTTP is faked via httpx.MockTransport and ADC
via a stubbed google.auth.default — no live Google calls. The handler routes the three
hops (signJwt -> token exchange -> Gmail send) by URL and records each request so the
JWT claims, the jwt-bearer exchange, and the final MIME message can be asserted."""

import base64
import json
from email import message_from_bytes
from types import SimpleNamespace

import httpx
import pytest

from usan_api import gmail_sender
from usan_api.gmail_sender import GmailMailer, GmailSendError
from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _settings(**over: str) -> Settings:
    return Settings(
        **_BASE,
        INVITE_EMAIL_ENABLED="true",
        INVITE_EMAIL_SENDER="noreply@usanretirement.com",
        INVITE_EMAIL_FROM_NAME="USAN Admin",
        **over,
    )


@pytest.fixture(autouse=True)
def _reset_caches():
    gmail_sender._adc_credentials = None
    gmail_sender._token_cache.clear()
    yield
    gmail_sender._adc_credentials = None
    gmail_sender._token_cache.clear()


def _stub_adc(monkeypatch):
    creds = SimpleNamespace(
        service_account_email="vm-sa@proj.iam.gserviceaccount.com",
        token="adc-bearer",
        valid=True,
        refresh=lambda request: None,
    )
    monkeypatch.setattr(gmail_sender.google.auth, "default", lambda scopes=None: (creds, "proj"))


def _install_handler(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        gmail_sender,
        "_build_client",
        lambda settings: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_send_mints_delegated_token_and_posts_message(monkeypatch):
    _stub_adc(monkeypatch)
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "signJwt" in request.url.path:
            seen["sign"] = request
            return httpx.Response(200, json={"signedJwt": "signed.delegated.jwt"})
        if request.url.host == "oauth2.googleapis.com":
            seen["token"] = request
            return httpx.Response(200, json={"access_token": "gmail-tok", "expires_in": 3600})
        if "gmail.googleapis.com" in request.url.host:
            seen["send"] = request
            return httpx.Response(200, json={"id": "msg-1"})
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)

    await GmailMailer(_settings()).send(
        to="manager@usanretirement.com",
        subject="You're invited to USAN Admin",
        text_body="Accept: http://app/v1/auth/accept-invite?token=TKN",
        html_body="<a href='http://app/v1/auth/accept-invite?token=TKN'>Accept</a>",
    )

    # signJwt: bearer is the ADC token; the payload claims impersonate the sender for gmail.send.
    sign = seen["sign"]
    assert sign.headers["Authorization"] == "Bearer adc-bearer"
    claims = json.loads(json.loads(sign.content)["payload"])
    assert claims["iss"] == "vm-sa@proj.iam.gserviceaccount.com"
    assert claims["sub"] == "noreply@usanretirement.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/gmail.send"
    assert claims["aud"] == "https://oauth2.googleapis.com/token"

    # token exchange: jwt-bearer grant carrying the signed assertion.
    token_body = dict(httpx.QueryParams(seen["token"].content.decode()))
    assert token_body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert token_body["assertion"] == "signed.delegated.jwt"

    # Gmail send: delegated bearer, correct user path, and a decodable MIME with the link.
    send = seen["send"]
    assert send.headers["Authorization"] == "Bearer gmail-tok"
    assert send.url.path == "/gmail/v1/users/noreply@usanretirement.com/messages/send"
    raw = json.loads(send.content)["raw"]
    mime = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert mime["To"] == "manager@usanretirement.com"
    assert mime["Subject"] == "You're invited to USAN Admin"
    assert "USAN Admin" in mime["From"]
    assert "noreply@usanretirement.com" in mime["From"]
    body = mime.as_string()
    assert "token=TKN" in body  # the accept link rides in the message


async def test_send_raises_gmailsenderror_on_http_error(monkeypatch):
    _stub_adc(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "signJwt" in request.url.path:
            return httpx.Response(200, json={"signedJwt": "s"})
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(500, json={"error": "boom"})  # Gmail send fails

    _install_handler(monkeypatch, handler)
    with pytest.raises(GmailSendError):
        await GmailMailer(_settings()).send(
            to="x@y.com", subject="s", text_body="t", html_body="<p>t</p>"
        )


async def test_signjwt_without_signedjwt_raises(monkeypatch):
    _stub_adc(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "signJwt" in request.url.path:
            return httpx.Response(200, json={})  # malformed: no signedJwt
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    _install_handler(monkeypatch, handler)
    with pytest.raises(GmailSendError):
        await GmailMailer(_settings()).send(
            to="x@y.com", subject="s", text_body="t", html_body="<p>t</p>"
        )


async def test_delegated_token_is_cached_across_sends(monkeypatch):
    _stub_adc(monkeypatch)
    counts = {"sign": 0, "token": 0, "send": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "signJwt" in request.url.path:
            counts["sign"] += 1
            return httpx.Response(200, json={"signedJwt": "s"})
        if request.url.host == "oauth2.googleapis.com":
            counts["token"] += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        counts["send"] += 1
        return httpx.Response(200, json={"id": "m"})

    _install_handler(monkeypatch, handler)
    mailer = GmailMailer(_settings())
    await mailer.send(to="a@y.com", subject="s", text_body="t", html_body="<p>t</p>")
    await mailer.send(to="b@y.com", subject="s", text_body="t", html_body="<p>t</p>")

    assert counts["send"] == 2  # both messages sent
    assert counts["sign"] == 1  # token minted once, then reused
    assert counts["token"] == 1
