"""Vertex text-embedding for KB ingestion (Phase 5). ADC + vertexai=True only — never the
Gemini Developer API. Regional client. Logs model + counts only, never chunk text."""

from __future__ import annotations

import asyncio

from google import genai
from google.genai import types
from loguru import logger

from usan_api.settings import Settings

_DIM = 768


def _embed_sync(texts: list[str], settings: Settings) -> list[list[float]]:
    client = genai.Client(
        vertexai=True, project=settings.gcp_project, location=settings.kb_embedding_location
    )
    try:
        resp = client.models.embed_content(
            model=settings.kb_embedding_model,
            contents=list(texts),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT", output_dimensionality=_DIM
            ),
        )
    finally:
        client.close()
    return [list(e.values or []) for e in (resp.embeddings or [])]


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
