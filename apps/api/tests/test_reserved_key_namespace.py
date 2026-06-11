"""B4: the `sched:`/`batch:` idempotency-key namespace is reserved for the materializer.

Spec §2.2 invariant 3: a squatted or colliding key could otherwise suppress or
substitute a wellness call, so `CreateCallRequest` rejects these prefixes with 422.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from usan_api.schemas.call import RESERVED_KEY_PREFIXES, CreateCallRequest


def test_create_call_request_rejects_sched_prefix() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateCallRequest(elder_id=uuid.uuid4(), idempotency_key="sched:xyz")
    assert "reserved" in str(exc_info.value)
    assert "sched:" in str(exc_info.value)


def test_create_call_request_rejects_batch_prefix() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateCallRequest(elder_id=uuid.uuid4(), idempotency_key="batch:1")
    assert "reserved" in str(exc_info.value)
    assert "batch:" in str(exc_info.value)


def test_create_call_request_accepts_non_prefix_match() -> None:
    # "scheduled-call-1" starts with "sched" but not the colon-prefixed namespace.
    req = CreateCallRequest(elder_id=uuid.uuid4(), idempotency_key="scheduled-call-1")
    assert req.idempotency_key == "scheduled-call-1"


def test_reserved_key_prefixes_constant() -> None:
    # Single source of truth: D7's materializer imports this constant.
    assert RESERVED_KEY_PREFIXES == ("sched:", "batch:")


def test_enqueue_call_endpoint_422_on_reserved_prefix(
    client: TestClient, operator_headers: dict[str, str]
) -> None:
    # FastAPI request validation must fire before the handler: never reaches the
    # DNC lock or elder lookup (a 404 on the unknown elder would mean it did).
    r = client.post(
        "/v1/calls",
        json={
            "elder_id": str(uuid.uuid4()),
            "idempotency_key": "batch:0001:1",
            "dynamic_vars": {},
        },
        headers=operator_headers,
    )
    assert r.status_code == 422
