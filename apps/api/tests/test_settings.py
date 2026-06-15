import pytest
from loguru import logger

from usan_api.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    # OPERATOR_API_KEY is now required; provide it for every settings test so the
    # cases below stay focused on the var each one is actually exercising.
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
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

    assert s.database_url.get_secret_value() == "postgresql://u:p@host/db"
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

    assert s.jwt_signing_key.get_secret_value() == "s" * 32


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


def _base_env(monkeypatch) -> None:
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
    }.items():
        monkeypatch.setenv(k, v)


def test_operator_api_key_required(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("OPERATOR_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPERATOR_API_KEY"):
        get_settings()


def test_operator_api_key_too_short_rejected(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("OPERATOR_API_KEY", "short")
    with pytest.raises(ValueError, match="OPERATOR_API_KEY"):
        get_settings()


def test_recording_ttl_max_is_one_hour(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RECORDING_SIGNED_URL_TTL_S", "3601")
    with pytest.raises(ValueError, match="RECORDING_SIGNED_URL_TTL_S"):
        get_settings()


def test_security_settings_defaults(monkeypatch):
    _base_env(monkeypatch)
    for var in ("RATE_LIMIT_ENABLED", "RATE_LIMIT_DEFAULT", "DOCS_ENABLED", "WEBHOOK_MAX_AGE_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PHI_RETENTION_DAYS", raising=False)
    s = Settings()
    assert s.rate_limit_enabled is True
    assert s.rate_limit_default == "60/minute"
    assert s.docs_enabled is False
    assert s.webhook_max_age_s == 300
    assert s.phi_retention_days is None


def test_security_settings_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "10/second")
    monkeypatch.setenv("DOCS_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_MAX_AGE_S", "120")
    monkeypatch.setenv("PHI_RETENTION_DAYS", "30")
    s = Settings()
    assert s.rate_limit_enabled is False
    assert s.rate_limit_default == "10/second"
    assert s.docs_enabled is True
    assert s.webhook_max_age_s == 120
    assert s.phi_retention_days == 30


def test_phi_retention_days_blank_coerces_to_none(monkeypatch):
    # Compose passes ${PHI_RETENTION_DAYS:-} as "" when the var is unset. Without
    # _blank_to_none the empty string fails int|None coercion and crash-loops the
    # API ("Invalid configuration for: PHI_RETENTION_DAYS"). Guards the #24 fix.
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_RETENTION_DAYS", "")
    s = get_settings()
    assert s.phi_retention_days is None


def test_phi_retention_days_whitespace_coerces_to_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_RETENTION_DAYS", "   ")
    s = get_settings()
    assert s.phi_retention_days is None


def test_db_tls_warning_for_remote_host_without_sslmode(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/d")
    s = Settings()
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        s.warn_if_db_tls_disabled()
    finally:
        logger.remove(handler_id)
    # Warns about unencrypted PHI and recommends the asyncpg-correct param
    # (ssl=require) — never libpq's sslmode=require, which asyncpg rejects at connect.
    assert any("PHI may transit unencrypted" in m for m in messages)
    assert any("ssl=require" in m for m in messages)
    assert not any("sslmode=require" in m for m in messages)


def test_db_tls_no_warning_for_local_or_sslmode(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/d")
    local = Settings()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/d?sslmode=require")
    remote_tls = Settings()
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        local.warn_if_db_tls_disabled()
        remote_tls.warn_if_db_tls_disabled()
    finally:
        logger.remove(handler_id)
    assert not messages


def test_db_tls_no_warning_for_asyncpg_ssl_param(monkeypatch):
    # asyncpg-native TLS uses ?ssl=require (not libpq's sslmode=); it must not trip
    # the PHI-in-transit warning, or operators learn to ignore a real one.
    _base_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com:5432/d?ssl=require")
    s = Settings()
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        s.warn_if_db_tls_disabled()
    finally:
        logger.remove(handler_id)
    assert not messages


# --- Clara Care Parity (002): poller flags + inbound key + med cap + Spanish ---


def test_clara_parity_settings_defaults(monkeypatch):
    _base_env(monkeypatch)
    for var in (
        "NOTIFICATION_OUTBOX_ENABLED",
        "NOTIFICATION_OUTBOX_POLL_INTERVAL_S",
        "CALLBACK_DIALER_POLLER_ENABLED",
        "CALLBACK_DIALER_POLL_INTERVAL_S",
        "FAMILY_REPORT_POLLER_ENABLED",
        "FAMILY_REPORT_POLL_INTERVAL_S",
        "TELNYX_INBOUND_PUBLIC_KEY",
        "MED_REASK_CAP",
        "SPANISH_PROFILE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    # Ship-inert: every new poller defaults OFF so merging changes no runtime behavior.
    assert s.notification_outbox_enabled is False
    assert s.callback_dialer_poller_enabled is False
    assert s.family_report_poller_enabled is False
    assert s.notification_outbox_poll_interval_s == 60
    assert s.callback_dialer_poll_interval_s == 60
    assert s.family_report_poll_interval_s == 3600
    assert s.telnyx_inbound_public_key is None
    assert s.med_reask_cap == 3
    assert s.spanish_profile_id is None


def test_clara_parity_settings_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_OUTBOX_POLL_INTERVAL_S", "30")
    monkeypatch.setenv("CALLBACK_DIALER_POLLER_ENABLED", "true")
    monkeypatch.setenv("FAMILY_REPORT_POLLER_ENABLED", "true")
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", "ed25519-pub")
    monkeypatch.setenv("MED_REASK_CAP", "5")
    monkeypatch.setenv("SPANISH_PROFILE_ID", "11111111-1111-1111-1111-111111111111")
    s = Settings()
    assert s.notification_outbox_enabled is True
    assert s.notification_outbox_poll_interval_s == 30
    assert s.callback_dialer_poller_enabled is True
    assert s.family_report_poller_enabled is True
    assert s.telnyx_inbound_public_key is not None
    assert s.telnyx_inbound_public_key.get_secret_value() == "ed25519-pub"
    assert s.med_reask_cap == 5
    assert s.spanish_profile_id == "11111111-1111-1111-1111-111111111111"


def test_notification_outbox_interval_capped_for_sc004(monkeypatch):
    # SC-004: a family alert must dispatch within 5 minutes. The poll interval is the
    # worst-case latency, so it is hard-capped at 300s — an over-budget value is rejected.
    _base_env(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_OUTBOX_POLL_INTERVAL_S", "301")
    with pytest.raises(ValueError, match="NOTIFICATION_OUTBOX_POLL_INTERVAL_S"):
        get_settings()


def test_med_reask_cap_out_of_range_rejected(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("MED_REASK_CAP", "0")
    with pytest.raises(ValueError, match="MED_REASK_CAP"):
        get_settings()


def test_spanish_profile_id_non_uuid_rejected_at_startup(monkeypatch):
    # A misconfigured non-UUID SPANISH_PROFILE_ID must fail fast at startup, not 500 on
    # every Spanish callback (set_spanish_callback parses it as the callback profile UUID).
    _base_env(monkeypatch)
    monkeypatch.setenv("SPANISH_PROFILE_ID", "not-a-uuid")
    with pytest.raises(ValueError, match="valid UUID"):
        Settings()


@pytest.mark.parametrize("blank", ["", "   "])
def test_inbound_key_and_spanish_profile_blank_coerce_to_none(monkeypatch, blank):
    # Compose passes unset optionals as "" (${VAR:-}); blank must coerce to None.
    _base_env(monkeypatch)
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", blank)
    monkeypatch.setenv("SPANISH_PROFILE_ID", blank)
    s = get_settings()
    assert s.telnyx_inbound_public_key is None
    assert s.spanish_profile_id is None
