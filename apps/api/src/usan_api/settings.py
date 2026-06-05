from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from loguru import logger
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: SecretStr = Field(..., min_length=1, alias="DATABASE_URL")
    livekit_api_key: str = Field(..., min_length=1, alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(..., min_length=32, alias="LIVEKIT_API_SECRET")
    livekit_url: str = Field(..., min_length=1, alias="LIVEKIT_URL")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    # Optional override: pin a specific LiveKit SIP outbound trunk ID (ST_...).
    # When unset, the API auto-provisions (or reuses) a trunk named
    # ``livekit_outbound_trunk_name`` from the Telnyx SIP credentials below, so
    # no environment-specific trunk ID needs to be configured by hand.
    livekit_sip_outbound_trunk_id: str | None = Field(
        default=None, alias="LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
    )
    livekit_outbound_trunk_name: str = Field(
        default="usan-telnyx-outbound", alias="LIVEKIT_OUTBOUND_TRUNK_NAME"
    )
    telnyx_caller_id: str | None = Field(default=None, alias="TELNYX_CALLER_ID")
    telnyx_sip_host: str = Field(default="sip.telnyx.com", alias="TELNYX_SIP_HOST")
    telnyx_sip_username: str | None = Field(default=None, alias="TELNYX_SIP_USERNAME")
    telnyx_sip_password: str | None = Field(default=None, alias="TELNYX_SIP_PASSWORD")
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
    outbound_ringing_timeout_s: int = Field(
        default=45, ge=5, le=120, alias="OUTBOUND_RINGING_TIMEOUT_S"
    )
    outbound_max_call_duration_s: int = Field(
        default=1800, ge=60, le=7200, alias="OUTBOUND_MAX_CALL_DURATION_S"
    )
    jwt_signing_key: SecretStr = Field(..., min_length=32, alias="JWT_SIGNING_KEY")
    # Static bearer token guarding the operator/management plane (elders, DNC, and
    # outbound call enqueue/lookup). Distinct from the agent's per-call JWTs. Held as
    # SecretStr so it is masked in repr()/model_dump()/tracebacks, never logged raw.
    operator_api_key: SecretStr = Field(..., min_length=16, alias="OPERATOR_API_KEY")
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
    recording_signed_url_ttl_s: int = Field(
        default=3600, ge=60, le=3600, alias="RECORDING_SIGNED_URL_TTL_S"
    )
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_default: str = Field(default="60/minute", alias="RATE_LIMIT_DEFAULT")
    docs_enabled: bool = Field(default=False, alias="DOCS_ENABLED")
    webhook_max_age_s: int = Field(default=300, ge=30, le=3600, alias="WEBHOOK_MAX_AGE_S")
    # When set, a background poller purges PHI (transcripts + terminal-call dynamic
    # vars) older than this many days. Default None means retention never runs, so
    # existing deployments are unaffected.
    phi_retention_days: int | None = Field(default=None, alias="PHI_RETENTION_DAYS")
    recording_reconcile_grace_s: int = Field(
        default=300, ge=60, le=3600, alias="RECORDING_RECONCILE_GRACE_S"
    )
    retry_poll_interval_s: int = Field(default=30, ge=5, le=300, alias="RETRY_POLL_INTERVAL_S")
    retry_batch_size: int = Field(default=20, ge=1, le=200, alias="RETRY_BATCH_SIZE")
    # Must exceed the ring timeout: a genuine in-flight dial leaves DIALING within
    # outbound_ringing_timeout_s, so a row still DIALING past this is stranded.
    retry_stuck_dialing_s: int = Field(default=300, ge=120, le=3600, alias="RETRY_STUCK_DIALING_S")
    retry_poller_enabled: bool = Field(default=True, alias="RETRY_POLLER_ENABLED")

    @field_validator(
        "telnyx_caller_id",
        "telnyx_sip_username",
        "telnyx_sip_password",
        "livekit_sip_outbound_trunk_id",
        # Compose passes PHI_RETENTION_DAYS as "" when unset (${VAR:-}); without
        # this, the empty string fails int|None coercion and crashes startup.
        # Blank => None => retention disabled, matching the documented default.
        "phi_retention_days",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # Compose passes unset optional vars as "" (e.g. ${VAR:-}); treat blank or
        # whitespace-only values as unset so truthiness checks behave correctly.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("livekit_url")
    @classmethod
    def _ws_scheme(cls, v: str) -> str:
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("must be a ws:// or wss:// URL")
        return v

    @property
    def database_url_async(self) -> str:
        """DATABASE_URL with the asyncpg driver, for SQLAlchemy's async engine."""
        url = self.database_url.get_secret_value()
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @property
    def livekit_http_url(self) -> str:
        """LIVEKIT_URL as an http(s) URL, for the livekit-api server SDK."""
        url = self.livekit_url
        if url.startswith("wss://"):
            return "https://" + url[len("wss://") :]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://") :]
        return url

    def warn_if_db_tls_disabled(self) -> None:
        """Warn (never fail) when a non-local DATABASE_URL has no TLS configured.

        Local/dev and the test container use plaintext loopback connections, so a
        missing TLS param there is expected. For a remote host it means PHI could
        cross the wire unencrypted — surface it loudly so operators notice. Both the
        libpq ``sslmode=`` and the asyncpg-native ``ssl=`` query params count as TLS
        configured; parsing the query (not a substring scan) also avoids a false
        positive when ``ssl`` appears in the password.
        """
        url = self.database_url.get_secret_value()
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if "sslmode" in query or "ssl" in query:
            return
        host = (parts.hostname or "").lower()
        if host in _LOCAL_HOSTS:
            return
        logger.bind(db_host=host).warning(
            "DATABASE_URL has no TLS query param and host {db_host} is not local; "
            "PHI may transit unencrypted — append ?ssl=require (asyncpg rejects sslmode=)",
            db_host=host,
        )


def _field_names(errors: list[Any], *, error_type: str | None = None) -> list[str]:
    """Field names (env-var aliases) from Pydantic errors, optionally filtered by type."""
    names = []
    for err in errors:
        if error_type is not None and err["type"] != error_type:
            continue
        loc = err["loc"]
        names.append(str(loc[0]) if loc else "<root>")
    return names


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # lru_cache never caches a raised exception, so a fixed environment is
    # picked up on the next call — do not wrap callers expecting otherwise.
    try:
        settings = Settings()
    except ValidationError as e:
        # Re-raise as ValueError naming the offending env vars, so the message
        # is stable and callers don't depend on Pydantic's internal wording.
        missing = _field_names(e.errors(), error_type="missing")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}") from e
        invalid = _field_names(e.errors())
        raise ValueError(f"Invalid configuration for: {', '.join(invalid)}") from e
    settings.warn_if_db_tls_disabled()
    return settings
