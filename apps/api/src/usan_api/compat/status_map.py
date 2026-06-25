"""CallStatus -> RetellAI call_status + disconnection_reason mapping (feature 003, data-model §4).

``dnc_blocked`` never appears on the wire: the compat create path raises an explicit 400
(stakeholder decision) before a DNC row would exist.
"""

from __future__ import annotations

from usan_api.db.base import CallStatus

# Native CallStatus -> RetellAI call_status. RetellAI's call_status enum is
# registered | not_connected | ongoing | ended | error. We deliberately never emit
# `not_connected` — native BUSY/NO_ANSWER reach the contact's carrier and are classified
# `ended` (with a dial_busy / dial_no_answer disconnection_reason). FROZEN (oracle):
# confirm the CRM does not require not_connected for unanswered/busy outcomes.
_STATUS_MAP: dict[CallStatus, str] = {
    CallStatus.QUEUED: "registered",
    CallStatus.DIALING: "registered",
    CallStatus.RINGING: "registered",
    CallStatus.IN_PROGRESS: "ongoing",
    CallStatus.COMPLETED: "ended",
    CallStatus.VOICEMAIL_LEFT: "ended",
    CallStatus.NO_ANSWER: "ended",
    CallStatus.BUSY: "ended",
    CallStatus.FAILED: "error",
    CallStatus.CANCELLED: "ended",
    CallStatus.DNC_BLOCKED: "error",  # never serialized (explicit 400 at create)
    CallStatus.REGISTERED: "registered",
}

# Per-terminal-status RetellAI disconnection_reason. Non-terminal statuses have none yet.
_DISCONNECT_MAP: dict[CallStatus, str] = {
    CallStatus.COMPLETED: "user_hangup",
    CallStatus.VOICEMAIL_LEFT: "voicemail_reached",
    CallStatus.NO_ANSWER: "dial_no_answer",
    CallStatus.BUSY: "dial_busy",
    CallStatus.FAILED: "dial_failed",  # FROZEN (oracle)
    CallStatus.CANCELLED: "manual_stopped",
}

# Terminal statuses RetellAI considers a "successful" outcome for call_analysis.call_successful.
_SUCCESS_STATUSES = frozenset({CallStatus.COMPLETED, CallStatus.VOICEMAIL_LEFT})


def to_call_status(status: CallStatus) -> str:
    return _STATUS_MAP.get(status, "error")


def to_disconnection_reason(status: CallStatus) -> str | None:
    # Mapped deterministically from status (RetellAI-valid vocabulary). The raw native
    # end_reason is intentionally NOT echoed — its values are not RetellAI's enum.
    return _DISCONNECT_MAP.get(status)


def is_terminal(status: CallStatus) -> bool:
    return status in _DISCONNECT_MAP


def call_successful(status: CallStatus) -> bool | None:
    # None while the call is still non-terminal (outcome unknown).
    if status not in _DISCONNECT_MAP:
        return None
    return status in _SUCCESS_STATUSES
