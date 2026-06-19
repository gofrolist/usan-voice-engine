"""Keyless Gmail send via Google Workspace domain-wide delegation (spec 2026-06-19).

The VM's attached service account (ADC) self-signs a delegated JWT for the configured
sender mailbox via IAM Credentials ``signJwt``, exchanges it for a ``gmail.send`` access
token (OAuth JWT-bearer grant), and POSTs a MIME message to the Gmail API. No private
key or password is stored — the same keyless trust the GCS signed-URL path relies on
(``serviceAccountTokenCreator`` on self). Every endpoint is a fixed Google host, so there
is no user-controlled URL and no SSRF surface. The ADC token, signed JWT, delegated
access token, and raw message body are NEVER logged.
"""

import asyncio
import base64
import json
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Protocol, cast
from urllib.parse import quote

import google.auth
import google.auth.credentials
import google.auth.exceptions
import google.auth.transport.requests
import httpx

from usan_api.settings import Settings

_CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (a public URL, not a secret)
_SIGN_JWT_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:signJwt"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/{sender}/messages/send"
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class GmailSendError(Exception):
    """Any failure minting a delegated token or sending a message via the Gmail API."""


class Mailer(Protocol):
    """Transport seam so callers (and tests) can swap the Gmail implementation."""

    async def send(self, *, to: str, subject: str, text_body: str, html_body: str) -> None: ...


# Cached ADC credentials for the IAM signJwt bearer. Refreshing hits the metadata server,
# so reuse the credentials object and refresh only when the token is missing or expired
# (~1h). Guarded by a lock because the refresh may run inside asyncio.to_thread workers.
# Mirrors object_storage._signing_creds — kept local rather than shared to avoid touching
# the PHI-signing path (whose tests patch its module internals directly; spec 2026-06-19).
_adc_credentials: google.auth.credentials.Credentials | None = None
_adc_lock = threading.Lock()

# Minted gmail.send access tokens, cached per sender until ~60s before expiry. A rare race
# under concurrent first-sends mints twice (both valid) — harmless, so no async lock here.
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def _adc_creds() -> google.auth.credentials.Credentials:
    """Return refreshed ADC scoped for cloud-platform (the signJwt bearer), cached."""
    global _adc_credentials
    with _adc_lock:
        if _adc_credentials is None:
            _adc_credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM])
        if not _adc_credentials.valid:
            _adc_credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
        return _adc_credentials


def _build_client(settings: Settings) -> httpx.AsyncClient:
    """The httpx client for the signJwt / token / Gmail calls (test seam, like webhook_delivery)."""
    return httpx.AsyncClient(timeout=settings.invite_email_timeout_s)


async def _sign_jwt(
    client: httpx.AsyncClient, sa_email: str, adc_token: str | None, claims: dict[str, Any]
) -> str:
    """Sign ``claims`` with the service account via IAM Credentials signJwt (keyless)."""
    resp = await client.post(
        _SIGN_JWT_URL.format(sa=quote(sa_email, safe="@")),
        headers={"Authorization": f"Bearer {adc_token}"},
        json={"payload": json.dumps(claims)},
    )
    resp.raise_for_status()
    signed = cast(dict[str, Any], resp.json()).get("signedJwt")
    if not signed:
        raise GmailSendError("signJwt response had no signedJwt")
    return str(signed)


async def _exchange_jwt(client: httpx.AsyncClient, signed_jwt: str) -> tuple[str, int]:
    """Exchange a signed delegated JWT for a gmail.send access token (jwt-bearer grant)."""
    resp = await client.post(
        _TOKEN_ENDPOINT,
        data={"grant_type": _JWT_BEARER_GRANT, "assertion": signed_jwt},
    )
    resp.raise_for_status()
    data = cast(dict[str, Any], resp.json())
    token = data.get("access_token")
    if not token:
        raise GmailSendError("token endpoint returned no access_token")
    return str(token), int(data.get("expires_in", 3600))


async def _delegated_token(client: httpx.AsyncClient, settings: Settings) -> str:
    """A cached gmail.send access token impersonating the configured sender mailbox."""
    sender = settings.invite_email_sender
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(sender)
        if cached is not None and cached[1] > now + 60:
            return cached[0]
    creds = await asyncio.to_thread(_adc_creds)
    sa_email = str(creds.service_account_email)  # type: ignore[attr-defined]
    claims = {
        "iss": sa_email,
        "sub": sender,
        "scope": _GMAIL_SEND_SCOPE,
        "aud": _TOKEN_ENDPOINT,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    signed = await _sign_jwt(client, sa_email, creds.token, claims)
    token, expires_in = await _exchange_jwt(client, signed)
    with _token_lock:
        _token_cache[sender] = (token, now + expires_in)
    return token


def _build_raw_message(
    *, from_addr: str, from_name: str, to: str, subject: str, text_body: str, html_body: str
) -> str:
    """A base64url-encoded multipart/alternative MIME message for the Gmail ``raw`` field."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailMailer:
    """Sends mail from the configured Workspace mailbox via keyless domain-wide delegation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, *, to: str, subject: str, text_body: str, html_body: str) -> None:
        settings = self._settings
        sender = settings.invite_email_sender
        raw = _build_raw_message(
            from_addr=sender,
            from_name=settings.invite_email_from_name,
            to=to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        try:
            async with _build_client(settings) as client:
                token = await _delegated_token(client, settings)
                resp = await client.post(
                    _GMAIL_SEND_URL.format(sender=quote(sender, safe="@")),
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw},
                )
                resp.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError, google.auth.exceptions.GoogleAuthError) as e:
            # Surface a single transport-level error type; the body is never included so a
            # token/JWT in an upstream message can't leak into a caller's log line.
            raise GmailSendError("Gmail send failed") from e
