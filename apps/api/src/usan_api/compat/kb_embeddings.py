"""Vertex text-embedding for KB ingestion (Phase 5). ADC + vertexai=True only — never the
Gemini Developer API. Regional client. Logs model + counts only, never chunk text."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

from google import genai
from google.genai import types
from loguru import logger

from usan_api.settings import Settings

_DIM = 768
_MAX_BATCH_TEXTS = 100
_MAX_BATCH_CHARS = 60_000


def _batches(texts: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    chars = 0
    for t in texts:
        if cur and (len(cur) >= _MAX_BATCH_TEXTS or chars + len(t) > _MAX_BATCH_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(t)
        chars += len(t)
    if cur:
        out.append(cur)
    return out


@contextmanager
def _vertex_client(settings: Settings) -> Generator[genai.Client]:
    client = genai.Client(
        vertexai=True, project=settings.gcp_project, location=settings.kb_embedding_location
    )
    try:
        yield client
    finally:
        client.close()


def _embed_sync(texts: list[str], settings: Settings) -> list[list[float]]:
    # auto_truncate=True: a single dense/CJK chunk can exceed text-embedding-005's ~2048-token
    # per-input limit even within the char-bounded chunk size. Let Vertex truncate the input
    # (graceful tail loss) rather than reject the whole batch and brick the KB.
    config = types.EmbedContentConfig(
        task_type="RETRIEVAL_DOCUMENT", output_dimensionality=_DIM, auto_truncate=True
    )
    vectors: list[list[float]] = []
    with _vertex_client(settings) as client:
        for batch in _batches(texts):
            resp = client.models.embed_content(
                model=settings.kb_embedding_model,
                contents=list(batch),
                config=config,
            )
            for e in resp.embeddings or []:
                vectors.append(list(e.values or []))
    return vectors


def _embed_query_sync(text: str, settings: Settings) -> list[float]:
    # RETRIEVAL_QUERY: asymmetric retrieval — the query is embedded differently from the stored
    # documents (which used RETRIEVAL_DOCUMENT). auto_truncate guards an over-long query turn.
    config = types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY", output_dimensionality=_DIM, auto_truncate=True
    )
    with _vertex_client(settings) as client:
        resp = client.models.embed_content(
            model=settings.kb_embedding_model, contents=[text], config=config
        )
        embeddings = resp.embeddings or []
        return list(embeddings[0].values or []) if embeddings else []


async def embed_query(text: str, settings: Settings) -> list[float]:
    """Embed one query string -> a 768-dim vector. Raises ValueError on an unexpected shape."""
    vector = await asyncio.to_thread(_embed_query_sync, text, settings)
    if len(vector) != _DIM:
        logger.bind(n_out=len(vector), model=settings.kb_embedding_model).error(
            "KB query embedding returned unexpected shape model={model}"
        )
        raise ValueError("query embedding shape mismatch")
    return vector


async def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    """Embed chunk texts -> 768-dim vectors (order-preserving). Empty input -> []."""
    if not texts:
        return []
    vectors = await asyncio.to_thread(_embed_sync, texts, settings)
    if len(vectors) != len(texts) or any(len(v) != _DIM for v in vectors):
        logger.bind(n_in=len(texts), n_out=len(vectors), model=settings.kb_embedding_model).error(
            "KB embedding returned unexpected shape model={model}"
        )
        raise ValueError("embedding shape mismatch")
    return vectors
