"""Agent-side receiver for mid-call dynamic-variable updates.

Wire contract (mirror of apps/api livekit_dispatch.py): LiveKit data-plane topic
``'usan/vars'``, RELIABLE delivery, JSON object of str->str. On receipt, merge into
the running vars, re-substitute the instruction template, and hand the new
instructions back via the ``on_update`` closure (which calls
``agent.update_instructions``).

This module is intentionally free of LiveKit imports so ``apply_dynamic_vars`` is
fully unit-testable without a running room. The worker supplies the ``on_update``
closure that connects this pure function to the live agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from loguru import logger

from usan_agent import prompt_vars

# Wire-contract constant — mirrors ``livekit_dispatch.DYNAMIC_VARS_TOPIC`` in
# apps/api. The two services share no Python imports, so this value is intentionally
# duplicated. Any rename must be applied on both sides simultaneously.
DYNAMIC_VARS_TOPIC = "usan/vars"


def apply_dynamic_vars(
    payload: bytes,
    *,
    current_vars: Mapping[str, str],
    template: str,
    on_update: Callable[[str], object],
) -> None:
    """Parse a dynamic-vars data packet and re-substitute the instruction template.

    Args:
        payload: Raw bytes from the LiveKit data packet (``DataPacket.data``).
        current_vars: A read-only snapshot of the vars map taken at registration time.
            Each incoming packet merges its vars over this snapshot independently —
            subsequent packets do NOT accumulate into ``current_vars`` (acceptable for
            v1, where the API broadcasts a full vars map per update).
        template: The raw (unsubstituted) instruction template string.
        on_update: Called with the newly substituted instruction text when the
            payload carries a non-empty update.  Typically a closure that calls
            ``agent.update_instructions(new_text)``.

    The function is a no-op for blank payloads, non-UTF-8 bytes, non-dict JSON,
    or empty dicts.  Malformed payloads are logged at WARNING level and silently
    discarded — they must never raise so a bad packet cannot crash the call.
    """
    if not payload or not payload.strip():
        return

    try:
        incoming = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        logger.warning("dynamic-vars: dropped malformed data packet (not valid JSON/UTF-8)")
        return

    if not isinstance(incoming, dict) or not incoming:
        return

    merged = {**current_vars, **{str(k): str(v) for k, v in incoming.items()}}
    on_update(prompt_vars.substitute(template, merged))
