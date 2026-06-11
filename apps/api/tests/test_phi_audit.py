"""Unit tests for ``usan_api.phi_audit`` — the locked-sink PHI-access log lines.

Pure loguru-capture tests (no DB): the locked 6-year Cloud Logging sink
(``infra/terraform/observability.tf:47``, bucket ``usan-phi-audit``) substring-matches
the two message constants, so they are pinned verbatim here, and the helpers' bind
shape is pinned against the operator plane's bit-identical contract (``actor`` bound
only when given).
"""

import uuid

from loguru import logger

from usan_api import phi_audit


def test_locked_sink_strings_verbatim():
    # Exact == pins, NOT substring checks: infra/terraform/observability.tf:47's locked
    # 6-year usan-phi-audit sink substring-matches these messages — any drift silently
    # breaks the immutable PHI-access trail. This is the ONE place outside phi_audit.py
    # where the strings may be retyped.
    assert phi_audit.TRANSCRIPT_ACCESSED == "Transcript accessed"
    assert phi_audit.RECORDING_URL_ACCESSED == "Recording URL accessed"


def test_transcript_accessed_binds_ids_and_actor_only_when_given():
    cid = uuid.uuid4()
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        phi_audit.log_transcript_accessed(call_id=cid, client="10.0.0.9", segments=3)

        assert len(records) == 1
        record = records[0]
        assert record["message"] == phi_audit.TRANSCRIPT_ACCESSED
        extra = record["extra"]
        assert extra["call_id"] == str(cid)
        assert extra["client"] == "10.0.0.9"
        assert extra["segments"] == 3
        # Operator-plane bit-identical contract: no actor key unless one was given.
        assert "actor" not in extra

        phi_audit.log_transcript_accessed(
            call_id=cid, client="10.0.0.9", segments=3, actor="nurse@usan.org"
        )

        assert len(records) == 2
        assert records[1]["message"] == phi_audit.TRANSCRIPT_ACCESSED
        assert records[1]["extra"]["actor"] == "nurse@usan.org"
    finally:
        logger.remove(handler_id)


def test_recording_url_accessed_binds_flag_and_actor():
    cid = uuid.uuid4()
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        phi_audit.log_recording_url_accessed(call_id=cid, client="10.0.0.9", actor="nurse@usan.org")

        assert len(records) == 1
        record = records[0]
        assert record["message"] == phi_audit.RECORDING_URL_ACCESSED
        extra = record["extra"]
        assert extra["call_id"] == str(cid)
        assert extra["client"] == "10.0.0.9"
        assert extra["has_recording"] is True
        assert extra["actor"] == "nurse@usan.org"

        phi_audit.log_recording_url_accessed(call_id=cid, client="10.0.0.9")

        assert len(records) == 2
        assert records[1]["message"] == phi_audit.RECORDING_URL_ACCESSED
        assert records[1]["extra"]["has_recording"] is True
        assert "actor" not in records[1]["extra"]
    finally:
        logger.remove(handler_id)
