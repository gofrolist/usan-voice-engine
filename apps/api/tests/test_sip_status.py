import pytest

from usan_api.db.base import CallStatus
from usan_api.sip_status import classify_dial_exception, sip_code_from_exception


class _FakeTwirp(Exception):
    def __init__(self, message, metadata=None):
        super().__init__(message)
        self.metadata = metadata or {}


def test_sip_code_from_metadata():
    exc = _FakeTwirp("dial failed", metadata={"sip_status_code": "486"})
    assert sip_code_from_exception(exc) == 486


def test_sip_code_from_message_fallback():
    exc = _FakeTwirp("upstream returned 480 Temporarily Unavailable")
    assert sip_code_from_exception(exc) == 480


def test_sip_code_none_when_absent():
    assert sip_code_from_exception(Exception("opaque error")) is None


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_reason"),
    [
        (486, CallStatus.BUSY, "sip_busy"),
        (408, CallStatus.NO_ANSWER, "sip_no_answer"),
        (480, CallStatus.NO_ANSWER, "sip_no_answer"),
        (487, CallStatus.NO_ANSWER, "sip_no_answer"),
        (603, CallStatus.NO_ANSWER, "sip_declined"),
        (404, CallStatus.FAILED, "sip_error"),
        (503, CallStatus.FAILED, "sip_error"),
    ],
)
def test_classify_by_sip_code(code, expected_status, expected_reason):
    exc = _FakeTwirp("x", metadata={"sip_status_code": str(code)})
    status, reason, error = classify_dial_exception(exc)
    assert status is expected_status
    assert reason == expected_reason
    assert error == {"sip_code": code}


def test_classify_timeout_is_no_answer():
    status, reason, error = classify_dial_exception(TimeoutError())
    assert status is CallStatus.NO_ANSWER
    assert reason == "ring_timeout"


def test_classify_unknown_is_failed():
    status, reason, error = classify_dial_exception(Exception("opaque"))
    assert status is CallStatus.FAILED
    assert reason == "dial_error"
    assert error == {"reason": "Exception"}
