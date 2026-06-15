"""Parsed inbound Telnyx SMS (US2 / T027).

The Telnyx ``message.received`` webhook wraps the message in ``data.payload``. This
module reduces that envelope to the few fields the intake needs, defensively (a
malformed or non-message event yields ``None`` rather than raising).
"""

from typing import Any

from pydantic import BaseModel

# Cap the stored inbound text: a signed Telnyx SMS is small, but bound it anyway so a
# malformed/oversized payload can't write an unbounded blob into family_tasks.message.
_MAX_SMS_TEXT_CHARS = 2000


class InboundSms(BaseModel):
    """The fields the family-task / opt-out intake needs from an inbound SMS."""

    message_id: str  # Telnyx message id — the idempotency key
    from_number: str  # E.164 sender; matched against family_contacts.phone_e164
    text: str
    event_type: str


def parse_inbound_sms(payload: dict[str, Any]) -> InboundSms | None:
    """Reduce a Telnyx webhook payload to an ``InboundSms``, or ``None`` if not handled.

    Returns None for non-``message.received`` events or payloads missing the message id
    or sender — the route treats that as an ignorable (200) delivery.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    event_type = data.get("event_type") or ""
    inner = data.get("payload")
    if not isinstance(inner, dict):
        return None
    message_id = inner.get("id") or data.get("id") or ""
    sender = inner.get("from")
    from_number = sender.get("phone_number", "") if isinstance(sender, dict) else ""
    text = inner.get("text") or ""
    if event_type != "message.received" or not message_id or not from_number:
        return None
    return InboundSms(
        message_id=str(message_id),
        from_number=str(from_number),
        text=str(text)[:_MAX_SMS_TEXT_CHARS],
        event_type=event_type,
    )
