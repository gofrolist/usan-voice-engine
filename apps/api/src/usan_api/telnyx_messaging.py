"""Telnyx Messaging API client (Phase 3 send_sms; design §6.5).

Mirrors oauth.py's raw-httpx + wrap-errors pattern (no SDK). One function:
``send_sms`` POSTs to /messages with the configured messaging profile and from
number, returns the Telnyx message id, and wraps any transport/HTTP/parse failure
in TelnyxMessagingError. The caller (sms_outbox) marks the row failed on raise.
"""

from typing import Any, cast

import httpx

from usan_api.settings import Settings


class TelnyxMessagingError(Exception):
    """Any failure sending an SMS via the Telnyx Messaging API."""


async def send_sms(settings: Settings, *, to_number: str, body: str) -> str:
    """Send one SMS; return the Telnyx message id. Raises TelnyxMessagingError.

    Requires telnyx_messaging_api_key / _profile_id / _from_number to be set (the
    caller gates on the feature flag, but a misconfigured flag-on/secret-missing
    combination still raises rather than silently sending half a request).
    """
    api_key = settings.telnyx_messaging_api_key
    if (
        api_key is None
        or not settings.telnyx_messaging_profile_id
        or not settings.telnyx_from_number
    ):
        raise TelnyxMessagingError("Telnyx messaging is not fully configured")
    url = f"{settings.telnyx_messaging_api_url}/messages"
    headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    payload = {
        "messaging_profile_id": settings.telnyx_messaging_profile_id,
        "from": settings.telnyx_from_number,
        "to": to_number,
        "text": body,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.telnyx_messaging_timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise TelnyxMessagingError("Telnyx message send failed") from exc
    message_id = (data.get("data") or {}).get("id")
    if not message_id:
        raise TelnyxMessagingError("Telnyx response had no message id")
    return str(message_id)
