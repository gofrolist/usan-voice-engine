from usan_api.compat.kb_chunking import chunk_text


def test_short_text_one_chunk() -> None:
    assert chunk_text("hello world", min_size=2, max_size=100) == ["hello world"]


def test_empty_is_no_chunks() -> None:
    assert chunk_text("   ", min_size=2, max_size=100) == []


def test_long_text_respects_max() -> None:
    body = " ".join(["word"] * 500)
    chunks = chunk_text(body, min_size=50, max_size=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks) == body.replace(" ", "")
