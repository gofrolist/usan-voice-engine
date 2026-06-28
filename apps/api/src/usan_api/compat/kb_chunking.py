"""Text chunking for KB ingestion (Phase 5). Char-based, honors [min_size, max_size]."""

from __future__ import annotations


def chunk_text(content: str, *, min_size: int, max_size: int) -> list[str]:
    text = content.strip()
    if not text:
        return []
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_size, n)
        if end < n:
            # Prefer to break on the last whitespace at/after min_size to avoid splitting words.
            window = text[start:end]
            cut = window.rfind(" ")
            if cut >= min_size:
                end = start + cut
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]
