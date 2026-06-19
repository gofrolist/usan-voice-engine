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
        api.TokenVerifier(
            settings.livekit_api_key.get_secret_value(),
            settings.livekit_api_secret.get_secret_value(),
        )
    )
    event = receiver.receive(body, auth_token)
    # Skip the age check only when there is genuinely no timestamp. ``> 0`` (not mere
    # truthiness) is deliberate: the protobuf default and a never-set field are both 0,
    # and a negative value is nonsensical — both should fail open on age, not raise.
    created_at = getattr(event, "created_at", 0)
    if created_at > 0:
        age_s = time.time() - created_at
        if age_s > settings.webhook_max_age_s:
            raise WebhookReplayError(
                f"webhook is {int(age_s)}s old (max {settings.webhook_max_age_s}s)"
            )
    return event
