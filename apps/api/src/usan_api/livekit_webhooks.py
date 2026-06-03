import time

from livekit import api

from usan_api.settings import Settings


class WebhookReplayError(Exception):
    """A signature-valid webhook whose creation timestamp is outside the replay window."""


def verify_livekit_webhook(body: str, auth_token: str, settings: Settings) -> api.WebhookEvent:
    """Verify a LiveKit webhook and return the event.

    Raises on invalid signature, and raises WebhookReplayError when the verified event
    carries a creation timestamp older than ``webhook_max_age_s`` — a stale, replayed
    delivery. Events without a usable timestamp skip the age check (fail open on that
    one dimension only; the signature is still required).
    """
    receiver = api.WebhookReceiver(
        api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    event = receiver.receive(body, auth_token)
    created_at = getattr(event, "created_at", 0)
    if created_at and time.time() - created_at > settings.webhook_max_age_s:
        raise WebhookReplayError(
            f"webhook is {int(time.time() - created_at)}s old (max {settings.webhook_max_age_s}s)"
        )
    return event
