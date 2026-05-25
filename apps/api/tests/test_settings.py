import pytest

from usan_api.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    s = get_settings()

    assert s.database_url == "postgresql://u:p@host/db"
    assert s.livekit_api_key == "key"
    assert s.livekit_api_secret == "a" * 32
    assert s.livekit_url == "ws://livekit:7880"
    assert s.log_level == "DEBUG"


def test_settings_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    with pytest.raises(ValueError, match="DATABASE_URL"):
        get_settings()


def test_settings_validates_secret_length(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "short")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    with pytest.raises(ValueError, match="at least 32 characters"):
        get_settings()
