"""T050 — audit-log fidelity: every PHI-touching compat operation emits a structured
org + op audit line, and NO phone number, contact name, external id, token, or other
PHI ever reaches the logs (Constitution II / VI, FR-055).

Captures the loguru stream around real compat requests (message + all bound ``extra``
fields) and asserts the audit op IS present while the PHI carried in the request is
absent — catching both bound-field and message-interpolated leaks.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

_PHONE = "+15557654321"
_NAME = "Jane Q Secretson"
_EXTERNAL_ID = "crm-secret-9001"


def _capture(fn):
    sink: list[str] = []
    handler_id = logger.add(
        lambda m: sink.append(str(m)), level="DEBUG", format="{message} {extra}"
    )
    try:
        fn()
    finally:
        logger.remove(handler_id)
    return "\n".join(sink)


def test_batch_create_audit_line_has_no_phi(compat_client, compat_headers):
    def _run():
        r = compat_client.post(
            "/create-batch-call",
            json={
                "from_number": "+15551230000",
                "tasks": [
                    {
                        "to_number": _PHONE,
                        "metadata": {"name": _NAME, "external_id": _EXTERNAL_ID},
                    }
                ],
            },
            headers=compat_headers,
        )
        assert r.status_code == 201

    text = _capture(_run)
    assert "create-batch-call" in text  # the structured audit op fired...
    assert "compat_org_id" in text  # ...with the org bound
    # ...and not a trace of the PHI that flowed through the request:
    assert _PHONE not in text
    assert _NAME not in text
    assert _EXTERNAL_ID not in text


def test_unexpected_500_logs_only_the_exception_type():
    # The catch-all handler must log the exception TYPE NAME only (never the message,
    # which could carry PHI) and return the fixed, PHI-free envelope.
    from usan_api.compat.errors import _handle_unexpected

    secret_in_message = f"boom {_PHONE} {_NAME}"

    def _run():
        resp = asyncio.run(_handle_unexpected(None, RuntimeError(secret_in_message)))
        assert resp.status_code == 500
        assert json.loads(resp.body) == {"status": 500, "message": "internal error"}

    text = _capture(_run)
    assert "RuntimeError" in text  # the type name is logged...
    assert _PHONE not in text  # ...but never the exception message's PHI
    assert _NAME not in text
