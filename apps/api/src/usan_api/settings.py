from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from loguru import logger
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
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
    # --- Admin UI / Google SSO (P3). All optional: SSO is off unless the OAuth
    # client id, secret, and redirect URI are all set (sso_enabled). Infra wiring
    # (Secret Manager + compose) is P5; the API boots fine without these.
    google_oauth_client_id: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="GOOGLE_OAUTH_CLIENT_SECRET"
    )
    google_oauth_redirect_uri: str | None = Field(default=None, alias="GOOGLE_OAUTH_REDIRECT_URI")
    # Optional G Suite hosted-domain restriction (the `hd` claim). When set, an ID
    # token whose hd != this value is rejected even if the email is allow-listed.
    google_oauth_hd: str | None = Field(default=None, alias="GOOGLE_OAUTH_HD")
    # Comma-separated emails seeded into admin_users (role=admin) on first boot.
    admin_bootstrap_emails: str = Field(default="", alias="ADMIN_BOOTSTRAP_EMAILS")
    # Session-cookie lifetime. Removal/role changes still take effect immediately
    # (require_admin_session re-checks the DB), so this only bounds re-login.
    admin_session_ttl_s: int = Field(default=28800, ge=300, le=86400, alias="ADMIN_SESSION_TTL_S")
    # Set the Secure flag on the session/tx cookies. Default True for prod (Caddy
    # terminates TLS). The test client + local http serve over http, where a Secure
    # cookie is never returned by the client, so they set this false.
    session_cookie_secure: bool = Field(default=True, alias="SESSION_COOKIE_SECURE")
    # Where /v1/auth/callback redirects the browser after a successful login (the SPA).
    admin_post_login_redirect: str = Field(default="/", alias="ADMIN_POST_LOGIN_REDIRECT")
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
    telnyx_per_min_usd: Decimal = Field(default=Decimal("0.008"), ge=0, alias="TELNYX_PER_MIN_USD")
    llm_input_per_1k_usd: Decimal = Field(default=Decimal("0"), ge=0, alias="LLM_INPUT_PER_1K_USD")
    llm_output_per_1k_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="LLM_OUTPUT_PER_1K_USD"
    )
    cartesia_stt_per_min_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="CARTESIA_STT_PER_MIN_USD"
    )
    cartesia_tts_per_1k_chars_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="CARTESIA_TTS_PER_1K_CHARS_USD"
    )
    gcs_storage_per_gb_month_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="GCS_STORAGE_PER_GB_MONTH_USD"
    )
    pricing_version: str = Field(default="2026-06-05", alias="PRICING_VERSION")

    # --- Telnyx Messaging (Phase 3 send_sms; design §6.6). Feature flag default
    # FALSE: SMS never fires until a deploy explicitly enables it. The 3 secret/
    # profile/from fields are blank-able (compose passes "" when unset).
    telnyx_messaging_api_key: SecretStr | None = Field(
        default=None, alias="TELNYX_MESSAGING_API_KEY"
    )
    telnyx_messaging_profile_id: str | None = Field(
        default=None, alias="TELNYX_MESSAGING_PROFILE_ID"
    )
    telnyx_from_number: str | None = Field(default=None, alias="TELNYX_FROM_NUMBER")
    telnyx_messaging_enabled: bool = Field(default=False, alias="TELNYX_MESSAGING_ENABLED")
    telnyx_messaging_api_url: str = Field(
        default="https://api.telnyx.com/v2", alias="TELNYX_MESSAGING_API_URL"
    )
    telnyx_messaging_timeout_s: int = Field(
        default=10, ge=1, le=60, alias="TELNYX_MESSAGING_TIMEOUT_S"
    )

    # --- Scheduler poller + concurrency gate (batch/scheduled calling, design §5.1).
    # Ship-inert contract: with both flags at their False defaults nothing dials
    # autonomously and the retry poller's claim behavior is bit-identical to today.
    # MAX_CONCURRENT_CALLS=8 is sized for the e2-standard-4 single-VM reality
    # (~5 simultaneous calls empirically saturated 2 vCPU; 8 on 4 vCPU is the
    # conservative start — measure before raising, per the v0.1.0 overwhelm lesson).
    # RESERVED_CONCURRENCY keeps headroom for ad-hoc/inbound calls, away from the
    # pollers. The daily cap of 2 allows the daily wellness schedule plus one batch
    # campaign per elder per day. AUTONOMOUS_DIALING_PAUSED is the state-preserving
    # emergency stop (§5.4, §10).
    scheduler_poller_enabled: bool = Field(default=False, alias="SCHEDULER_POLLER_ENABLED")
    scheduler_poll_interval_s: int = Field(
        default=60, ge=15, le=600, alias="SCHEDULER_POLL_INTERVAL_S"
    )
    scheduler_batch_size: int = Field(default=50, ge=1, le=500, alias="SCHEDULER_BATCH_SIZE")
    concurrency_gate_enabled: bool = Field(default=False, alias="CONCURRENCY_GATE_ENABLED")
    max_concurrent_calls: int = Field(default=8, ge=1, le=50, alias="MAX_CONCURRENT_CALLS")
    reserved_concurrency: int = Field(default=2, ge=0, le=20, alias="RESERVED_CONCURRENCY")
    max_autonomous_calls_per_elder_per_day: int = Field(
        default=2, ge=1, le=10, alias="MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY"
    )
    autonomous_dialing_paused: bool = Field(default=False, alias="AUTONOMOUS_DIALING_PAUSED")

    @model_validator(mode="after")
    def _reserved_below_max(self) -> Settings:
        # The gate computes max - reserved - in_flight; reserved >= max means the
        # autonomous planes can never dial (spec §5.1).
        if self.reserved_concurrency >= self.max_concurrent_calls:
            raise ValueError("RESERVED_CONCURRENCY must be < MAX_CONCURRENT_CALLS")
        return self

    @model_validator(mode="after")
    def _scheduler_requires_gate(self) -> Settings:
        # Staged-enable order (spec §10.3): the gate is the hard dial cap; the
        # scheduler must never materialize-and-dial without it. Gate-only is the
        # documented intermediate state (pre-enable observability); scheduler-only
        # is a misconfiguration — fail at startup, not on the first dial burst.
        if self.scheduler_poller_enabled and not self.concurrency_gate_enabled:
            raise ValueError(
                "SCHEDULER_POLLER_ENABLED=true requires CONCURRENCY_GATE_ENABLED=true "
                "(the scheduler must never run without the hard dial cap; spec §10.3)"
            )
        return self

    @field_validator(
        "telnyx_caller_id",
        "telnyx_sip_username",
        "telnyx_sip_password",
        "livekit_sip_outbound_trunk_id",
        "google_oauth_client_id",
        "google_oauth_redirect_uri",
        "google_oauth_hd",
        # Compose passes PHI_RETENTION_DAYS as "" when unset (${VAR:-}); without
        # this, the empty string fails int|None coercion and crashes startup.
        # Blank => None => retention disabled, matching the documented default.
        "phi_retention_days",
        "telnyx_messaging_api_key",
        "telnyx_messaging_profile_id",
        "telnyx_from_number",
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

    @field_validator("telnyx_messaging_api_url")
    @classmethod
    def _https_scheme(cls, v: str) -> str:
        # The SMS flush POSTs the rendered body + the elder's phone to this URL, so a
        # plaintext/hostile endpoint would leak PHI. Require https:// (operator config).
        if not v.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return v

    @field_validator("admin_post_login_redirect")
    @classmethod
    def _relative_redirect(cls, v: str) -> str:
        # Operator config, not user input, so not an attacker-driven open redirect — but
        # validate it is a site-relative path (not absolute/protocol-relative) so a
        # misconfiguration can't bounce operators off-site right after login.
        if not v.startswith("/") or v.startswith("//"):
            raise ValueError("ADMIN_POST_LOGIN_REDIRECT must be a relative path starting with '/'")
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

    @property
    def sso_enabled(self) -> bool:
        """True when Google SSO is fully configured (client id + secret + redirect)."""
        secret = (
            self.google_oauth_client_secret.get_secret_value()
            if self.google_oauth_client_secret is not None
            else ""
        )
        return bool(self.google_oauth_client_id and secret and self.google_oauth_redirect_uri)

    @property
    def bootstrap_emails_list(self) -> list[str]:
        """Allow-list bootstrap emails, trimmed + lowercased, blanks dropped."""
        return [e.strip().lower() for e in self.admin_bootstrap_emails.split(",") if e.strip()]

    def warn_if_db_tls_disabled(self) -> None:
        """Warn (never fail) when a non-local DATABASE_URL has no TLS configured.

        Local/dev and the test container use plaintext loopback connections, so a
        missing TLS param there is expected. For a remote host it means PHI could
        cross the wire unencrypted — surface it loudly so operators notice. Both the
        libpq ``sslmode=`` and the asyncpg-native ``ssl=`` query params suppress the
        warning (parsing the query, not a substring scan, also avoids a false positive
        when ``ssl`` appears in the password). Note the app connects via asyncpg, which
        accepts ``ssl=`` but rejects ``sslmode=`` at connect time — so the remediation
        message recommends ``ssl=require``, the param that actually works here.
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
