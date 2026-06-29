import uuid

import pytest
from pydantic import ValidationError

from usan_api.schemas.tools import RetrieveKbContextRequest, RetrieveKbContextResponse


def test_retrieve_kb_context_request_carries_call_id_and_query():
    cid = uuid.uuid4()
    req = RetrieveKbContextRequest(call_id=cid, query="how do I reset my pin")
    assert req.call_id == cid
    assert req.query == "how do I reset my pin"


def test_retrieve_kb_context_request_rejects_overlong_query():
    with pytest.raises(ValidationError):
        RetrieveKbContextRequest(call_id=uuid.uuid4(), query="x" * 4001)


def test_retrieve_kb_context_request_allows_empty_query():
    # Empty is allowed at the schema layer; retrieve_context's blank-query gate returns empty.
    req = RetrieveKbContextRequest(call_id=uuid.uuid4(), query="")
    assert req.query == ""


def test_retrieve_kb_context_response_shape():
    resp = RetrieveKbContextResponse(context="some context", hit_count=2)
    assert resp.context == "some context"
    assert resp.hit_count == 2
