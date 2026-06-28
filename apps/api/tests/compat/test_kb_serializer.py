import uuid

from usan_api.compat import ids
from usan_api.compat.kb_serializer import serialize_kb
from usan_api.db.models import KnowledgeBase, KnowledgeBaseSource

from .conformance import assert_conforms, assert_sdk_roundtrip


def _kb(status: str) -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.id = uuid.uuid4()
    kb.name = "Support KB"
    kb.status = status
    kb.max_chunk_size = 2000
    kb.min_chunk_size = 400
    kb.enable_auto_refresh = False
    return kb


def _src() -> KnowledgeBaseSource:
    s = KnowledgeBaseSource()
    s.id = uuid.uuid4()
    s.source_type = "text"
    s.title = "FAQ"
    s.content = "secret body"
    s.content_url = "https://internal/kb-source/x"
    return s


def test_in_progress_omits_sources() -> None:
    body = serialize_kb(_kb("in_progress"), [_src()]).model_dump(exclude_none=True)
    assert "knowledge_base_sources" not in body
    assert body["status"] == "in_progress"
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_complete_includes_text_source_and_never_raw_text() -> None:
    body = serialize_kb(_kb("complete"), [_src()]).model_dump(exclude_none=True)
    assert body["knowledge_base_sources"][0]["type"] == "text"
    assert body["knowledge_base_sources"][0]["source_id"].startswith("source_")
    assert "secret body" not in str(body)  # raw content never echoed
    assert_conforms(body, "KnowledgeBaseResponse")
    assert_sdk_roundtrip(body, "retell.types:KnowledgeBaseResponse")


def test_kb_id_roundtrip_and_bad_prefix() -> None:
    import pytest

    from usan_api.compat.errors import CompatError

    kid = uuid.uuid4()
    assert ids.decode_kb_id(ids.encode_kb_id(kid)) == kid
    sid = uuid.uuid4()
    assert ids.decode_kb_source_id(ids.encode_kb_source_id(sid)) == sid
    with pytest.raises(CompatError) as exc_info:
        ids.decode_kb_id("agent_" + kid.hex)
    assert exc_info.value.status_code == 422
