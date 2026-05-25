import pytest

from usan_agent.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice-uuid")

    s = get_settings()

    assert s.livekit_api_key == "key"
    assert s.cartesia_api_key == "cart-key"
    assert s.gemini_api_key == "gemini-key"
    assert s.default_cartesia_voice_id == "voice-uuid"
    assert s.log_level == "INFO"


def test_settings_requires_cartesia_key(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice-uuid")

    with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
        get_settings()
