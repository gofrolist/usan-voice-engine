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
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

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
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        get_settings()


def test_settings_validates_secret_length(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "short")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    with pytest.raises(ValueError, match="LIVEKIT_API_SECRET"):
        get_settings()


def test_database_url_async_converts_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_database_url_async_converts_legacy_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_database_url_async_leaves_asyncpg_untouched(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.database_url_async == "postgresql+asyncpg://u:p@host/db"


def test_livekit_http_url_converts_ws(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.livekit_http_url == "https://livekit:7880"


def test_outbound_fields_default_none_and_agent_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.delenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", raising=False)
    monkeypatch.delenv("TELNYX_CALLER_ID", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    s = Settings()

    assert s.livekit_sip_outbound_trunk_id is None
    assert s.telnyx_caller_id is None
    assert s.agent_name == "usan-agent"


def test_outbound_autoprovision_fields_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.delenv("TELNYX_SIP_USERNAME", raising=False)
    monkeypatch.delenv("TELNYX_SIP_PASSWORD", raising=False)
    monkeypatch.delenv("TELNYX_SIP_HOST", raising=False)
    monkeypatch.delenv("LIVEKIT_OUTBOUND_TRUNK_NAME", raising=False)

    s = Settings()

    assert s.telnyx_sip_username is None
    assert s.telnyx_sip_password is None
    assert s.telnyx_sip_host == "sip.telnyx.com"
    assert s.livekit_outbound_trunk_name == "usan-telnyx-outbound"


def test_outbound_autoprovision_fields_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("TELNYX_SIP_USERNAME", "user1")
    monkeypatch.setenv("TELNYX_SIP_PASSWORD", "pass1")
    monkeypatch.setenv("TELNYX_SIP_HOST", "sip.example.com")
    monkeypatch.setenv("LIVEKIT_OUTBOUND_TRUNK_NAME", "custom-trunk")

    s = Settings()

    assert s.telnyx_sip_username == "user1"
    assert s.telnyx_sip_password == "pass1"
    assert s.telnyx_sip_host == "sip.example.com"
    assert s.livekit_outbound_trunk_name == "custom-trunk"


def test_outbound_dial_timeouts_have_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.delenv("OUTBOUND_RINGING_TIMEOUT_S", raising=False)
    monkeypatch.delenv("OUTBOUND_MAX_CALL_DURATION_S", raising=False)

    s = Settings()

    assert s.outbound_ringing_timeout_s == 45
    assert s.outbound_max_call_duration_s == 1800


def test_outbound_dial_timeouts_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OUTBOUND_RINGING_TIMEOUT_S", "30")

    s = Settings()

    assert s.outbound_ringing_timeout_s == 30


def test_jwt_signing_key_required(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)

    get_settings.cache_clear()
    with pytest.raises(ValueError, match="JWT_SIGNING_KEY"):
        get_settings()


def test_jwt_signing_key_loads(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)

    s = Settings()

    assert s.jwt_signing_key == "s" * 32


def test_retry_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.delenv("RETRY_POLL_INTERVAL_S", raising=False)
    monkeypatch.delenv("RETRY_BATCH_SIZE", raising=False)
    monkeypatch.delenv("RETRY_STUCK_DIALING_S", raising=False)
    monkeypatch.delenv("RETRY_POLLER_ENABLED", raising=False)

    s = Settings()

    assert s.retry_poll_interval_s == 30
    assert s.retry_batch_size == 20
    assert s.retry_stuck_dialing_s == 300
    assert s.retry_poller_enabled is True


def test_retry_settings_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("RETRY_POLL_INTERVAL_S", "15")
    monkeypatch.setenv("RETRY_BATCH_SIZE", "5")
    monkeypatch.setenv("RETRY_STUCK_DIALING_S", "600")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")

    s = Settings()

    assert s.retry_poll_interval_s == 15
    assert s.retry_batch_size == 5
    assert s.retry_stuck_dialing_s == 600
    assert s.retry_poller_enabled is False


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("RETRY_POLL_INTERVAL_S", "4"),
        ("RETRY_POLL_INTERVAL_S", "301"),
        ("RETRY_BATCH_SIZE", "0"),
        ("RETRY_BATCH_SIZE", "201"),
        ("RETRY_STUCK_DIALING_S", "119"),
        ("RETRY_STUCK_DIALING_S", "3601"),
    ],
)
def test_retry_settings_out_of_range_rejected(monkeypatch, var, value):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv(var, value)

    with pytest.raises(ValueError, match=var):
        get_settings()


def test_recording_settings_defaults(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@h:5432/d",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    from usan_api.settings import Settings

    s = Settings()
    assert s.gcs_bucket is None
    assert s.recording_signed_url_ttl_s == 3600


def test_recording_settings_from_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@h:5432/d",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "GCS_BUCKET": "usan-rec",
        "RECORDING_SIGNED_URL_TTL_S": "600",
    }.items():
        monkeypatch.setenv(k, v)
    from usan_api.settings import Settings

    s = Settings()
    assert s.gcs_bucket == "usan-rec"
    assert s.recording_signed_url_ttl_s == 600
