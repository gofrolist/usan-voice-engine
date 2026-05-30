"""Classify a LiveKit SIP outbound-dial failure into a CallStatus.

LiveKit raises an error from create_sip_participant(wait_until_answered=True) when
the callee is busy / does not answer / rejects. The error carries the upstream SIP
status code, but the exact attribute is version-volatile, so sip_code_from_exception
parses defensively (metadata keys first, then the message). Verify the real shape
against a live busy/no-answer before relying on the metadata path.
"""

import asyncio
import re
from typing import Any

from usan_api.db.base import CallStatus

_CODE_RE = re.compile(r"\b([4-6]\d\d)\b")
_META_KEYS = ("sip_status_code", "sip_status", "sipStatusCode", "status_code")


def sip_code_from_exception(exc: BaseException) -> int | None:
    """Best-effort extraction of the upstream SIP status code (e.g. 486)."""
    meta = getattr(exc, "metadata", None)
    if isinstance(meta, dict):
        for key in _META_KEYS:
            value = meta.get(key)
            if value is not None:
                try:
                    return int(str(value)[:3])
                except ValueError:
                    pass
    match = _CODE_RE.search(str(exc))
    return int(match.group(1)) if match else None


def classify_dial_exception(
    exc: BaseException,
) -> tuple[CallStatus, str, dict[str, Any]]:
    """Map a dial failure to (status, end_reason, error_dict)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return CallStatus.NO_ANSWER, "ring_timeout", {"reason": "timeout"}

    code = sip_code_from_exception(exc)
    if code == 486:
        return CallStatus.BUSY, "sip_busy", {"sip_code": code}
    if code in (408, 480, 487):
        return CallStatus.NO_ANSWER, "sip_no_answer", {"sip_code": code}
    if code == 603:
        return CallStatus.NO_ANSWER, "sip_declined", {"sip_code": code}
    if code is not None:
        return CallStatus.FAILED, "sip_error", {"sip_code": code}

    return CallStatus.FAILED, "dial_error", {"reason": type(exc).__name__}
