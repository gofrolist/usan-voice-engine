"""Phase 5b — retrieve_context gating, assembly, char-cap, PHI-safe logging."""

from __future__ import annotations

import io
import uuid

import pytest
from loguru import logger

from usan_api.compat import ids, kb_retrieval
from usan_api.compat.kb_retrieval import RetrievedContext, retrieve_context
from usan_api.repositories.knowledge_bases import ChunkHit
from usan_api.settings import Settings, get_settings


def _settings(**over) -> Settings:
    base = {"kb_retrieval_enabled": True, "gcp_project": "test-project"}
    base.update(over)
    return get_settings().model_copy(update=base)


_KB = ids.encode_kb_id(uuid.uuid4())


@pytest.mark.asyncio
async def test_gating_returns_empty(monkeypatch) -> None:
    async def _boom(*a, **k):  # must never be called when gated
        raise AssertionError("embed_query called while gated")

    monkeypatch.setattr(kb_retrieval, "embed_query", _boom)

    # flag off
    r = await retrieve_context(None, _settings(kb_retrieval_enabled=False), kb_ids=[_KB], query="q")
    assert r == RetrievedContext("", 0)
    # no gcp_project
    r = await retrieve_context(None, _settings(gcp_project=None), kb_ids=[_KB], query="q")
    assert r == RetrievedContext("", 0)
    # no kb_ids
    r = await retrieve_context(None, _settings(), kb_ids=[], query="q")
    assert r == RetrievedContext("", 0)
    # blank query
    r = await retrieve_context(None, _settings(), kb_ids=[_KB], query="   ")
    assert r == RetrievedContext("", 0)


@pytest.mark.asyncio
async def test_assembles_block_and_counts(monkeypatch) -> None:
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="alpha", distance=0.1),
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="beta", distance=0.2),
        ]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)
    r = await retrieve_context(None, _settings(), kb_ids=[_KB], query="q")
    assert r.hit_count == 2
    assert "alpha" in r.text
    assert "beta" in r.text


@pytest.mark.asyncio
async def test_char_cap_stops_before_exceeding(monkeypatch) -> None:
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="A" * 50, distance=0.1),
            ChunkHit(knowledge_base_id=uuid.uuid4(), content="B" * 50, distance=0.2),
        ]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)
    r = await retrieve_context(
        None, _settings(kb_retrieval_max_context_chars=60), kb_ids=[_KB], query="q"
    )
    assert "A" * 50 in r.text  # first chunk fits
    assert "B" * 50 not in r.text  # second would exceed 60 -> dropped
    assert len(r.text) <= 60


@pytest.mark.asyncio
async def test_logs_are_phi_safe(monkeypatch) -> None:
    # loguru does NOT propagate to pytest caplog by default.
    # Capture via a loguru sink into a StringIO instead.
    async def _embed(text, settings):
        return [0.1] * 768

    async def _search(db, *, kb_ids, query_embedding, limit, max_distance):
        return [ChunkHit(knowledge_base_id=uuid.uuid4(), content="SECRET_PHI_TEXT", distance=0.1)]

    monkeypatch.setattr(kb_retrieval, "embed_query", _embed)
    monkeypatch.setattr(kb_retrieval, "search_chunks", _search)

    buf = io.StringIO()
    handler_id = logger.add(buf, level="DEBUG")
    try:
        await retrieve_context(None, _settings(), kb_ids=[_KB], query="SECRET_QUERY_TEXT")
    finally:
        logger.remove(handler_id)

    blob = buf.getvalue()
    assert "SECRET_PHI_TEXT" not in blob
    assert "SECRET_QUERY_TEXT" not in blob
