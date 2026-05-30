"""Seam between the calls router and the background SIP dial.

Kept as a one-function module so the router can be unit-tested by monkeypatching
schedule_dial without spawning a real LiveKit dial.
"""

import uuid

from usan_api import background, livekit_dispatch
from usan_api.settings import Settings


def schedule_dial(call_id: uuid.UUID, settings: Settings) -> None:
    background.spawn(livekit_dispatch.dial_and_classify(call_id, settings))
