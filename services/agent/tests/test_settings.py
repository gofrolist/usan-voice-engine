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
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice-uuid")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = get_settings()

    assert s.livekit_api_key == "key"
    assert s.cartesia_api_key == "cart-key"
    assert s.gcp_project == "usan-retirement"
    assert s.default_cartesia_voice_id == "voice-uuid"
    assert s.log_level == "INFO"


def test_settings_vertex_fields(monkeypatch):
    # The LLM moved to Vertex AI (BAA-covered): GEMINI_API_KEY is gone, replaced by
    # GCP_PROJECT (required, for ADC) + VERTEX_LOCATION (defaulted). See Plan 4e A1.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")

    s = get_settings()

    assert s.gcp_project == "usan-retirement"
    assert s.vertex_location == "global"  # default (gemini-3.1-flash-lite is global-only on Vertex)
    assert not hasattr(s, "gemini_api_key")


def test_settings_requires_gcp_project(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    with pytest.raises(ValueError, match="GCP_PROJECT"):
        get_settings()


def test_settings_requires_cartesia_key(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")
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
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "voice")
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.api_base_url == "http://api:8000"
    assert s.jwt_signing_key == "s" * 32


def _base_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("CARTESIA_API_KEY", "cart")
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")
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


def test_api_base_url_rejects_external_http(monkeypatch):
    # PHI must not cross an untrusted network in the clear: a non-local plaintext http
    # host is rejected at startup, not merely warned.
    _base_env(monkeypatch)
    monkeypatch.setenv("API_BASE_URL", "http://api.usan.example.com")
    with pytest.raises(ValueError, match="API_BASE_URL"):
        get_settings()
