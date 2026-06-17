"""Invite-link URL construction (P3). The accept link points at the API accept
endpoint (served from the same public origin as the SPA via Caddy's /v1 proxy)."""

from urllib.parse import quote, urlsplit

from usan_api.settings import Settings


def _origin(settings: Settings) -> str:
    base = settings.admin_base_url or settings.google_oauth_redirect_uri or ""
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}"


def build_accept_url(settings: Settings, token: str) -> str:
    """The link an admin copies; opening it bounces through Google OAuth to accept."""
    return f"{_origin(settings)}/v1/auth/accept-invite?token={quote(token, safe='')}"


def build_accept_error_url(settings: Settings, reason: str) -> str:
    """Where the callback redirects the browser when an invite can't be accepted."""
    return f"{_origin(settings)}/accept-invite?status=error&reason={quote(reason, safe='')}"
