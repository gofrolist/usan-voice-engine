import pytest

from usan_api.compat import status_map
from usan_api.db.base import CallStatus

pytestmark = pytest.mark.frozen


def test_busy_and_no_answer_map_to_ended() -> None:
    assert status_map.to_call_status(CallStatus.BUSY) == "ended"
    assert status_map.to_call_status(CallStatus.NO_ANSWER) == "ended"
    assert status_map.to_disconnection_reason(CallStatus.BUSY) == "dial_busy"
    assert status_map.to_disconnection_reason(CallStatus.NO_ANSWER) == "dial_no_answer"


def test_not_connected_is_never_emitted() -> None:
    assert "not_connected" not in {status_map.to_call_status(s) for s in CallStatus}


def test_failed_maps_to_dial_failed() -> None:
    assert status_map.to_disconnection_reason(CallStatus.FAILED) == "dial_failed"
