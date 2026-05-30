import pytest

from usan_api.settings import Settings, get_settings


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

    with pytest.raises(ValueError, match="LIVEKIT_API_SECRET"):
        get_settings()


def test_database_url_async_converts_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_database_url_async_converts_legacy_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_database_url_async_leaves_asyncpg_untouched(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_livekit_http_url_converts_ws(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit:7880")

    s = Settings()

    assert s.livekit_http_url == "https://livekit:7880"


def test_outbound_fields_default_none_and_agent_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.delenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", raising=False)
    monkeypatch.delenv("TELNYX_CALLER_ID", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    s = Settings()

    assert s.livekit_sip_outbound_trunk_id is None
    assert s.telnyx_caller_id is None
    assert s.agent_name == "usan-agent"
