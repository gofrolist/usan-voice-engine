"""Contract freeze for knowledge-bases (RetellAI parity Phase 5).

Create/get/list responses must conform to the pinned oracle schema and round-trip
through the retell SDK KnowledgeBaseResponse model.
"""

from __future__ import annotations

import json

from .conformance import assert_conforms, assert_sdk_roundtrip


def _create(compat_client, headers):
    return compat_client.post(
        "/create-knowledge-base",
        data={
            "knowledge_base_name": "Support",
            "knowledge_base_texts": json.dumps([{"title": "FAQ", "text": "hi"}]),
        },
        headers=headers,
    )


def test_create_conforms(compat_client, compat_headers) -> None:
    body = _create(compat_client, compat_headers).json()
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_get_conforms(compat_client, compat_headers) -> None:
    kid = _create(compat_client, compat_headers).json()["knowledge_base_id"]
    body = compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).json()
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_list_each_element_conforms(compat_client, compat_headers) -> None:
    _create(compat_client, compat_headers)
    arr = compat_client.get("/list-knowledge-bases", headers=compat_headers).json()
    assert isinstance(arr, list)
    assert arr
    for item in arr:
        assert_conforms(item, "KnowledgeBaseResponse")
        assert_sdk_roundtrip(item, "retell.types:KnowledgeBaseResponse")
