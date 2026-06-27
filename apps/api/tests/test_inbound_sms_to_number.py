"""parse_inbound_sms captures the recipient (payload.to) for 4b-2 session matching."""

from __future__ import annotations

from usan_api.schemas.inbound_sms import parse_inbound_sms


def _payload(*, with_to: bool):
    inner = {"id": "m1", "from": {"phone_number": "+15551234567"}, "text": "hello"}
    if with_to:
        inner["to"] = [{"phone_number": "+15550000000"}]
    return {"data": {"event_type": "message.received", "payload": inner}}


def test_captures_to_number():
    parsed = parse_inbound_sms(_payload(with_to=True))
    assert parsed is not None
    assert parsed.to_number == "+15550000000"
    assert parsed.from_number == "+15551234567"


def test_to_number_absent_defaults_empty():
    parsed = parse_inbound_sms(_payload(with_to=False))
    assert parsed is not None
    assert parsed.to_number == ""
