import pytest

from usan_agent.settings import Settings, get_settings


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
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

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
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
        get_settings()


def test_api_callback_settings_load(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.api_base_url == "http://api:8000"
    assert s.jwt_signing_key == "s" * 32
    assert s.outbound_answer_timeout_s == 50


def _base_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)


def test_api_base_url_rejects_non_http_scheme(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("API_BASE_URL", "ftp://api:8000")
    with pytest.raises(ValueError, match="API_BASE_URL"):
        get_settings()


def test_api_base_url_internal_http_default_works(monkeypatch):
    # The internal Docker-bridge default must keep working over plaintext http.
    _base_env(monkeypatch)
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    assert get_settings().api_base_url == "http://api:8000"


def test_api_base_url_https_external_works(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("API_BASE_URL", "https://api.usan.example.com")
    assert get_settings().api_base_url == "https://api.usan.example.com"


def test_api_base_url_warns_on_external_http(monkeypatch, caplog):
    import logging

    from usan_agent import settings as settings_mod

    _base_env(monkeypatch)
    monkeypatch.setenv("API_BASE_URL", "http://api.usan.example.com")

    handler_id = settings_mod.logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        with caplog.at_level(logging.WARNING):
            assert get_settings().api_base_url == "http://api.usan.example.com"
    finally:
        settings_mod.logger.remove(handler_id)

    assert any("plaintext http" in r.message for r in caplog.records)
