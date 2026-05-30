from livekit import api

from usan_api.settings import Settings


def verify_livekit_webhook(body: str, auth_token: str, settings: Settings) -> api.WebhookEvent:
    """Verify a LiveKit webhook and return the event. Raises on invalid signature."""
    receiver = api.WebhookReceiver(
        api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    return receiver.receive(body, auth_token)
