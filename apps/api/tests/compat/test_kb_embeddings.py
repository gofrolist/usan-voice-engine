import pytest

from usan_api import settings as settings_mod
from usan_api.compat import kb_embeddings


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
