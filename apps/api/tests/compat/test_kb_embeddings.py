import pytest

from usan_api import settings as settings_mod
from usan_api.compat import kb_embeddings
from usan_api.compat.kb_embeddings import _batches


def _settings(**over):
    base = settings_mod.get_settings()
    return base.model_copy(update={"gcp_project": "p", **over})


@pytest.mark.asyncio
async def test_embed_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(kb_embeddings, "_embed_sync", lambda texts, s: [[0.0] * 768 for _ in texts])
    out = await kb_embeddings.embed_texts(["a", "b"], _settings())
    assert len(out) == 2
    assert all(len(v) == 768 for v in out)


@pytest.mark.asyncio
async def test_embed_shape_mismatch_raises(monkeypatch) -> None:
    monkeypatch.setattr(kb_embeddings, "_embed_sync", lambda texts, s: [[0.0] * 10])
    with pytest.raises(ValueError, match="embedding shape mismatch"):
        await kb_embeddings.embed_texts(["a"], _settings())


@pytest.mark.asyncio
async def test_embed_empty() -> None:
    assert await kb_embeddings.embed_texts([], _settings()) == []


def test_embed_sync_sets_auto_truncate(monkeypatch) -> None:
    """A single over-token (dense/CJK) chunk must NOT brick the KB: we ask Vertex to truncate
    the input rather than reject the batch. Pin auto_truncate=True on the embed config."""
    captured: dict = {}

    class _FakeEmbedding:
        values = [0.0] * 768

    class _FakeResp:
        embeddings = [_FakeEmbedding()]

    class _FakeModels:
        def embed_content(self, *, model, contents, config):
            captured["config"] = config
            return _FakeResp()

    class _FakeClient:
        models = _FakeModels()

        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(kb_embeddings.genai, "Client", _FakeClient)
    kb_embeddings._embed_sync(["x"], _settings())
    assert captured["config"].auto_truncate is True


def test_batches_split_by_count_and_chars() -> None:
    # 250 short texts -> 3 batches of <=100 (ceil(250/100)=3)
    short = ["x"] * 250
    batches = _batches(short)
    assert len(batches) == 3
    assert all(len(b) <= 100 for b in batches)
    assert [t for b in batches for t in b] == short

    # A few large texts (50_000 chars each) — each must flush into its own batch
    big = ["a" * 50_000] * 4
    batches2 = _batches(big)
    # Each big text is 50k chars; 2 would be 100k > _MAX_BATCH_CHARS=60_000, so each is alone
    assert len(batches2) == 4
    assert all(len(b) == 1 for b in batches2)
    assert [t for b in batches2 for t in b] == big

    # Mixed: verify total texts preserved in order
    mixed = [str(i) for i in range(300)]
    flat = [t for b in _batches(mixed) for t in b]
    assert flat == mixed
