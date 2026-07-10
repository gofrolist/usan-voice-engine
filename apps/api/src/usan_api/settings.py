import uuid
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Build provenance, baked into the image as ENV by apps/api/Dockerfile (CI passes the
    # git tag as VERSION and the commit as GIT_SHA). Surfaced via /health and /v1/auth/me
    # so the admin UI can show the deployed version. Defaults "dev" for local/uncontainerized runs.
    app_version: str = Field(default="dev", alias="APP_VERSION")
    git_sha: str = Field(default="dev", alias="GIT_SHA")

    database_url: SecretStr = Field(..., min_length=1, alias="DATABASE_URL")
    # Slug of the implicit default organization (multi-tenant foundation P1). The
    # tenant-context resolver maps every request to this single seeded org until P2
    # introduces per-user org resolution; behavior is unchanged with one org.
    default_org_slug: str = Field(default="usan", alias="DEFAULT_ORG_SLUG")
    # Both halves of the LiveKit credential pair are held as SecretStr so neither the
    # signing secret nor the paired key lands unmasked in repr()/model_dump()/tracebacks
    # (the key is half a credential and must not be logged either). Call sites use
    # .get_secret_value() (livekit_webhooks, livekit_dispatch).
    livekit_api_key: SecretStr = Field(..., min_length=1, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr = Field(..., min_length=32, alias="LIVEKIT_API_SECRET")
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
    # SecretStr: the SIP trunk password authenticates outbound calling and must never
    # surface in repr()/logs/tracebacks. Blank => None via _blank_to_none below; the one
    # consumer (livekit_dispatch trunk provisioning) calls .get_secret_value().
    telnyx_sip_password: SecretStr | None = Field(default=None, alias="TELNYX_SIP_PASSWORD")
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
    outbound_ringing_timeout_s: int = Field(
        default=45, ge=5, le=120, alias="OUTBOUND_RINGING_TIMEOUT_S"
    )
    outbound_max_call_duration_s: int = Field(
        default=1800, ge=60, le=7200, alias="OUTBOUND_MAX_CALL_DURATION_S"
    )
    jwt_signing_key: SecretStr = Field(..., min_length=32, alias="JWT_SIGNING_KEY")
    # Static bearer token guarding the operator/management plane (contacts, DNC, and
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
    invite_ttl_hours: int = Field(default=168, ge=1, le=720, alias="INVITE_TTL_HOURS")
    # Absolute public origin of the admin app, used to build invite accept links. When
    # unset, the origin is derived from GOOGLE_OAUTH_REDIRECT_URI (already configured for
    # SSO) — so prod needs no new env var. If set, must be absolute (http(s)://host).
    admin_base_url: str | None = Field(default=None, alias="ADMIN_BASE_URL")
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
    # PHI minimization: a GCS signed recording URL is an IP-unbound bearer token for
    # call audio, so the operator-plane TTL defaults to 10 min (the same ceiling the
    # admin plane already caps at). Raise RECORDING_SIGNED_URL_TTL_S only if a machine
    # integration genuinely needs a longer fetch window; the 60–3600s range is retained.
    recording_signed_url_ttl_s: int = Field(
        default=600, ge=60, le=3600, alias="RECORDING_SIGNED_URL_TTL_S"
    )
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_default: str = Field(default="60/minute", alias="RATE_LIMIT_DEFAULT")
    # Tighter, dedicated bucket for the pre-auth /v1/auth/* endpoints (login/callback):
    # credential-stuffing and OAuth-state probing must not share the broad operator
    # budget that bulk reads can exhaust. Keyed per client IP, like the default bucket.
    rate_limit_auth: str = Field(default="10/minute", alias="RATE_LIMIT_AUTH")
    # Per-call ceiling on the agent tool plane (/v1/tools/*), keyed on the call_id JWT
    # claim. The tool routes are excluded from the operator middleware so a busy call is
    # never throttled; this bound only trips a runaway/looping or hijacked agent token
    # (a legitimate call makes far fewer tool invocations per minute).
    tool_call_rate: str = Field(default="120/minute", alias="TOOL_CALL_RATE")
    # Comma-separated allow-list of immediate-peer IPs permitted to set X-Forwarded-For
    # (the reverse proxy hop, e.g. the Caddy container). When EMPTY the legacy behavior
    # holds: the XFF first hop is trusted unconditionally (correct only because Caddy +
    # Cloudflare AOP mTLS front the API in prod). Set it to the proxy's peer IP(s) to
    # reject spoofed XFF from any other origin (defense-in-depth for the rate-limit key
    # and the PHI audit trail). See client_ip.py.
    trusted_proxy_ips: str = Field(default="", alias="TRUSTED_PROXY_IPS")
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
    # Phase 4b-2: gate the inbound two-way SMS auto-reply engine independently of the
    # outbound send flag, so it can be staged/rolled-back on its own. Default FALSE.
    telnyx_inbound_sms_reply_enabled: bool = Field(
        default=False, alias="TELNYX_INBOUND_SMS_REPLY_ENABLED"
    )
    # Phase 4b-3: gate the unknown-recipient inbound SMS auto-create path independently of
    # the reply engine, so it can be staged/rolled-back on its own. Default FALSE.
    telnyx_inbound_sms_autocreate_enabled: bool = Field(
        default=False, alias="TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED"
    )
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
    # pollers. The daily cap bounds TOTAL autonomous roots/contact/day across every
    # source (both US5 wellness slots — morning + evening — AND batch campaigns).
    # At the default of 2 a single-slot contact gets one wellness call plus one batch
    # campaign; a two-slot contact's morning+evening fill the cap, so a same-day batch
    # to that contact is intentionally capped out (the harassment guard working as
    # designed). A slot beyond the cap skips observably (skipped_daily_cap) and
    # retries next day — never a silent drop, never a late dial. Raise the cap to
    # leave batch headroom for two-slot contacts; lower it (>=1) only if one autonomous
    # call/contact/day is the intended ceiling. AUTONOMOUS_DIALING_PAUSED is the
    # state-preserving emergency stop (§5.4, §10).
    scheduler_poller_enabled: bool = Field(default=False, alias="SCHEDULER_POLLER_ENABLED")
    scheduler_poll_interval_s: int = Field(
        default=60, ge=15, le=600, alias="SCHEDULER_POLL_INTERVAL_S"
    )
    scheduler_batch_size: int = Field(default=50, ge=1, le=500, alias="SCHEDULER_BATCH_SIZE")
    concurrency_gate_enabled: bool = Field(default=False, alias="CONCURRENCY_GATE_ENABLED")
    max_concurrent_calls: int = Field(default=8, ge=1, le=50, alias="MAX_CONCURRENT_CALLS")
    reserved_concurrency: int = Field(default=2, ge=0, le=20, alias="RESERVED_CONCURRENCY")
    max_autonomous_calls_per_contact_per_day: int = Field(
        default=2, ge=1, le=10, alias="MAX_AUTONOMOUS_CALLS_PER_CONTACT_PER_DAY"
    )
    autonomous_dialing_paused: bool = Field(default=False, alias="AUTONOMOUS_DIALING_PAUSED")

    # --- Outbound event webhooks (Phase A3, webhooks design §5.1). The deliberate
    # WEBHOOK_DELIVERY_ prefix keeps this namespace disjoint from the INBOUND
    # LiveKit-verification WEBHOOK_MAX_AGE_S above. Ship-inert: the flag defaults
    # False, gating only the delivery half of the always-on poller (housekeeping
    # runs regardless). No startup cross-field validator is needed — signing
    # secrets are per-endpoint rows, so flag-on alone is a valid configuration.
    webhook_delivery_enabled: bool = Field(default=False, alias="WEBHOOK_DELIVERY_ENABLED")
    webhook_delivery_poll_interval_s: int = Field(
        default=10, ge=5, le=300, alias="WEBHOOK_DELIVERY_POLL_INTERVAL_S"
    )
    webhook_delivery_timeout_s: int = Field(
        default=10, ge=1, le=60, alias="WEBHOOK_DELIVERY_TIMEOUT_S"
    )
    # Hard cap on how many response bytes we buffer from a customer-controlled receiver
    # endpoint. The status code is all we use; a malicious/buggy endpoint that streams an
    # unbounded body could otherwise OOM the API process (delivery runs many groups
    # concurrently). 64 KiB is ample for any real ack body. See webhook_delivery.deliver_one.
    webhook_delivery_max_response_bytes: int = Field(
        default=65536, ge=1024, le=1048576, alias="WEBHOOK_DELIVERY_MAX_RESPONSE_BYTES"
    )
    webhook_delivery_circuit_breaker_threshold: int = Field(
        default=10, ge=1, le=100, alias="WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD"
    )

    # --- Voice sample proxy (Cartesia TTS) + text-test LLM (Vertex AI). All optional
    # like gcs_bucket/telnyx_* above: the API boots without them; the consuming
    # endpoints (admin voice-sample, admin profile test/llm) check presence and fail
    # with a clear error when unconfigured. CARTESIA_API_KEY is the only secret and
    # stays server-side (the browser never calls Cartesia directly). The text-test
    # path authenticates to Vertex via ADC (not the Gemini Developer API), so it needs
    # the project + region only — never an API key (Constitution II PHI containment).
    cartesia_api_key: SecretStr | None = Field(default=None, alias="CARTESIA_API_KEY")
    cartesia_api_url: str = Field(default="https://api.cartesia.ai", alias="CARTESIA_API_URL")
    cartesia_version: str = Field(default="2024-11-13", alias="CARTESIA_VERSION")
    cartesia_sample_model: str = Field(default="sonic-2", alias="CARTESIA_SAMPLE_MODEL")
    gcp_project: str | None = Field(default=None, alias="GCP_PROJECT")
    vertex_location: str = Field(default="global", alias="VERTEX_LOCATION")
    # Post-call summarization + fact extraction (US4 / FR-024). Ship-inert: default OFF,
    # so no Vertex call (spend or PHI egress) happens until a deploy explicitly enables it
    # AND gcp_project is set. Reuses the text-test Vertex ADC path (Constitution II) — never
    # the Gemini Developer API. The model is a Vertex Gemini id (model_catalog provider).
    summarization_enabled: bool = Field(default=False, alias="SUMMARIZATION_ENABLED")
    summarization_model: str = Field(default="gemini-2.5-flash", alias="SUMMARIZATION_MODEL")
    # Post-chat analysis (Phase 4c-2 / rerun-chat-analysis). Ship-inert: default OFF, so no
    # Vertex call (spend or PHI egress) until a deploy enables it AND gcp_project is set.
    # Reuses the Vertex ADC path (Constitution II) — never the Gemini Developer API.
    chat_analysis_enabled: bool = Field(default=False, alias="CHAT_ANALYSIS_ENABLED")
    chat_analysis_model: str = Field(default="gemini-2.5-flash", alias="CHAT_ANALYSIS_MODEL")

    # Knowledge-base ingestion (Phase 5). All ship-inert: default OFF, so no Vertex embed
    # (spend or PHI egress) and no poller until a deploy enables them AND gcp_project is set.
    # Vertex text-embedding-005 is REGIONAL — kb_embedding_location must be a region, not
    # "global". Dimension 768 is baked into the Vector(768) column (model change -> migration).
    kb_embedding_enabled: bool = Field(default=False, alias="KB_EMBEDDING_ENABLED")
    kb_embedding_model: str = Field(default="text-embedding-005", alias="KB_EMBEDDING_MODEL")
    kb_embedding_location: str = Field(default="us-central1", alias="KB_EMBEDDING_LOCATION")
    kb_ingestion_poller_enabled: bool = Field(default=False, alias="KB_INGESTION_POLLER_ENABLED")
    kb_ingestion_poll_interval_s: int = Field(default=15, alias="KB_INGESTION_POLL_INTERVAL_S")
    kb_ingestion_batch_size: int = Field(default=10, alias="KB_INGESTION_BATCH_SIZE")
    kb_ingestion_lease_seconds: int = Field(default=300, alias="KB_INGESTION_LEASE_SECONDS")
    # Bounded-attempts auto-retry for transient embed failures (429/503/timeout). A failure
    # returns the KB to in_progress + increments ingestion_attempts (the lease provides backoff
    # before re-claim); at this many failures the KB is set terminal 'error'.
    kb_ingestion_max_attempts: int = Field(default=3, alias="KB_INGESTION_MAX_ATTEMPTS")

    # Knowledge-base text-RAG retrieval (Phase 5b). Ship-inert: default OFF, so no query
    # embed (spend or PHI egress) until a deploy enables it AND gcp_project is set. Reuses
    # kb_embedding_model / kb_embedding_location for the query embed. max_distance is a cosine
    # DISTANCE ceiling (0=identical, 2=opposite) — the relevance floor; 0.7 is a permissive
    # starting default that MUST be tuned against real KB content (model-specific distribution).
    kb_retrieval_enabled: bool = Field(default=False, alias="KB_RETRIEVAL_ENABLED")
    kb_retrieval_top_k: int = Field(default=5, ge=1, le=50, alias="KB_RETRIEVAL_TOP_K")
    kb_retrieval_max_distance: float = Field(
        default=0.7, ge=0.0, le=2.0, alias="KB_RETRIEVAL_MAX_DISTANCE"
    )
    kb_retrieval_max_context_chars: int = Field(
        default=8000, ge=1, alias="KB_RETRIEVAL_MAX_CONTEXT_CHARS"
    )
    # Knowledge-base VOICE retrieval (Phase 5c). Separate from kb_retrieval_enabled so the
    # latency-sensitive live-voice path can be rolled out independently of chat. Ship-inert:
    # default OFF, so the voice retrieval endpoint embeds/searches nothing until a deploy
    # enables it AND gcp_project is set. Reuses kb_retrieval_top_k / max_distance /
    # max_context_chars / kb_embedding_* (one tuning set for both channels).
    kb_retrieval_voice_enabled: bool = Field(default=False, alias="KB_RETRIEVAL_VOICE_ENABLED")

    # Conversation-flow DAG runtime for chat/SMS (Phase 6-runtime-chat). When on, a chat/SMS
    # agent bound to a RUNNABLE conversation flow executes the flow turn-by-turn instead of the
    # single system-prompt turn; a non-runnable flow falls back to the single-prompt path.
    flow_runtime_enabled: bool = Field(default=False, alias="FLOW_RUNTIME_ENABLED")

    # Conversation-flow DAG runtime for VOICE calls (Phase 6-runtime-voice). When on, a voice
    # agent bound to a RUNNABLE conversation flow is steered node-by-node via
    # POST /v1/runtime/flow-advance; a non-runnable/absent binding falls back to the single
    # static prompt. Independent of flow_runtime_enabled (chat) so voice and chat enable separately.
    flow_runtime_voice_enabled: bool = Field(default=False, alias="FLOW_RUNTIME_VOICE_ENABLED")

    # --- Clara Care Parity (feature 002): three new poller phases + inbound-SMS
    # verification + Spanish callback. All ship-inert: every new poller defaults OFF,
    # so merging changes NO runtime behavior until a deploy explicitly enables them
    # (mirrors the scheduler/webhook-delivery flag discipline above). The notification
    # outbox interval is capped at 300s so a family alert (crisis/missed-call) is always
    # dispatched within the 5-minute budget (SC-004). TELNYX_INBOUND_PUBLIC_KEY verifies
    # the inbound Telnyx SMS webhook (US2/US7); blank => None (compose passes "").
    notification_outbox_enabled: bool = Field(default=False, alias="NOTIFICATION_OUTBOX_ENABLED")
    notification_outbox_poll_interval_s: int = Field(
        default=60, ge=5, le=300, alias="NOTIFICATION_OUTBOX_POLL_INTERVAL_S"
    )
    callback_dialer_poller_enabled: bool = Field(
        default=False, alias="CALLBACK_DIALER_POLLER_ENABLED"
    )
    callback_dialer_poll_interval_s: int = Field(
        default=60, ge=5, le=300, alias="CALLBACK_DIALER_POLL_INTERVAL_S"
    )
    family_report_poller_enabled: bool = Field(default=False, alias="FAMILY_REPORT_POLLER_ENABLED")
    family_report_poll_interval_s: int = Field(
        default=3600, ge=60, le=86400, alias="FAMILY_REPORT_POLL_INTERVAL_S"
    )
    # Ed25519 public key used to verify inbound Telnyx webhook signatures. Held as
    # SecretStr so it never lands in repr()/logs even though a public key is not secret.
    telnyx_inbound_public_key: SecretStr | None = Field(
        default=None, alias="TELNYX_INBOUND_PUBLIC_KEY"
    )
    # How many times a not-taken medication is re-asked before the reminder is capped
    # and a routine follow_up_flag is raised instead of nagging further (US3 / FR-019).
    med_reask_cap: int = Field(default=3, ge=1, le=10, alias="MED_REASK_CAP")
    # Agent profile id (UUID string) configured for Spanish-language callbacks (US8 /
    # FR-030). Blank => None: a Spanish callback is then created but flagged for an
    # operator rather than auto-dialed with a Spanish profile.
    spanish_profile_id: str | None = Field(default=None, alias="SPANISH_PROFILE_ID")

    # --- Invite email delivery via Google Workspace (spec 2026-06-19). Ship-inert:
    # default OFF, so merging changes NO runtime behavior — invites stay copy-link-only
    # until a deploy flips the flag (after the one-time domain-wide-delegation setup).
    # Keyless: the sender is impersonated via the VM service account (IAM signJwt), so
    # there is NO secret here — only the flag + the mailbox identity. When the flag is
    # ON, INVITE_EMAIL_SENDER must name a real Workspace mailbox (the DWD `sub`).
    invite_email_enabled: bool = Field(default=False, alias="INVITE_EMAIL_ENABLED")
    invite_email_sender: str = Field(
        default="noreply@usanretirement.com", alias="INVITE_EMAIL_SENDER"
    )
    invite_email_from_name: str = Field(default="USAN Admin", alias="INVITE_EMAIL_FROM_NAME")
    invite_email_timeout_s: int = Field(default=10, ge=1, le=60, alias="INVITE_EMAIL_TIMEOUT_S")

    # --- RetellAI-compatible public API (feature 003-retellai-api-parity). Additive +
    # ship-inert: the compat sub-app is always mounted, but every endpoint requires a
    # compat_api_keys bearer token (none issued => 401), so merging changes NO reachable
    # behavior until an operator issues a key. COMPAT_DOCS_ENABLED toggles the RetellAI
    # OpenAPI/docs independently of the native DOCS_ENABLED. COMPAT_WEBHOOK_ALLOWED_HOSTS
    # is the attested in-infrastructure allow-list permitted to receive full-fidelity
    # (PHI-bearing) webhooks (Constitution II); empty => no compat webhook ever carries
    # PHI off-box. COMPAT_DEFAULT_TIMEZONE drives quiet-hours for the Contacts the compat
    # layer lazily upserts on number-first call/batch create (RetellAI has no Contact/
    # timezone concept). COMPAT_KEY_RATE_LIMIT is the CRM key's dedicated elevated bucket
    # (FR-054), disjoint from the operator/auth buckets.
    compat_docs_enabled: bool = Field(default=False, alias="COMPAT_DOCS_ENABLED")
    compat_webhook_allowed_hosts: str = Field(default="", alias="COMPAT_WEBHOOK_ALLOWED_HOSTS")
    compat_default_timezone: str = Field(
        default="America/New_York", alias="COMPAT_DEFAULT_TIMEZONE"
    )
    compat_key_rate_limit: str = Field(default="600/minute", alias="COMPAT_KEY_RATE_LIMIT")
    # Surface 3 external HTTP agent tools (design 2026-07-09). Ship-inert:
    # COMPAT_EXTERNAL_TOOLS_ENABLED gates ingest (create/update-retell-llm translates
    # general_tools) AND the runtime projection + execution proxy — off keeps general_tools
    # echo-only exactly as before. COMPAT_TOOL_ALLOWED_HOSTS is the attested allow-list a
    # client tool url may egress to (PHI-bearing args, Constitution II); empty => no external
    # tool validates. COMPAT_TOOL_CALLER_SECRET is the shared per-org X-Caller-Secret (= the
    # client's RETELL_FUNCTION_SECRET); it never leaves apps/api (never projected to the worker).
    compat_external_tools_enabled: bool = Field(
        default=False, alias="COMPAT_EXTERNAL_TOOLS_ENABLED"
    )
    compat_tool_allowed_hosts: str = Field(default="", alias="COMPAT_TOOL_ALLOWED_HOSTS")
    compat_tool_caller_secret: str | None = Field(default=None, alias="COMPAT_TOOL_CALLER_SECRET")
    # Ship-inert flag for the compat (RetellAI) webhook delivery poller (US2). Like
    # WEBHOOK_DELIVERY_ENABLED, it gates only the claim+POST half: the poller task always
    # starts so housekeeping (sweep/expire/prune) + the backlog gauge run flag-independently.
    # The poller reuses the native WEBHOOK_DELIVERY_* timeout/interval/breaker/max-bytes knobs.
    compat_webhook_delivery_enabled: bool = Field(
        default=False, alias="COMPAT_WEBHOOK_DELIVERY_ENABLED"
    )

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

    @model_validator(mode="after")
    def _invite_email_requires_sender(self) -> Settings:
        # The sender mailbox is the domain-wide-delegation `sub` (the impersonated
        # Workspace account). Sending with a blank sender would 400 at the Gmail API on
        # every invite — fail at startup instead of on the first admin click.
        if self.invite_email_enabled and not self.invite_email_sender.strip():
            raise ValueError(
                "INVITE_EMAIL_ENABLED=true requires INVITE_EMAIL_SENDER to name a real "
                "Workspace mailbox (the domain-wide-delegation subject; spec 2026-06-19)"
            )
        return self

    @model_validator(mode="after")
    def _external_tools_require_allowlist(self) -> Settings:
        # Fail-closed (Surface 3 WS-C): enabling client HTTP tools with an empty egress allow-list
        # would let every ingest 422 ("host not in allow-list") and every proxy call 502 — a
        # silently-broken feature. That combination is a misconfiguration; fail at startup, not
        # per call.
        if self.compat_external_tools_enabled and not self.compat_tool_allowed_hosts.strip():
            raise ValueError(
                "COMPAT_EXTERNAL_TOOLS_ENABLED=true requires COMPAT_TOOL_ALLOWED_HOSTS to name "
                "at least one egress host (Surface 3 WS-C)"
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
        # Compose passes these unset optionals as "" (${VAR:-}); blank => None.
        "cartesia_api_key",
        "gcp_project",
        # Clara Care Parity (002): inbound-SMS verification key + Spanish profile id.
        "telnyx_inbound_public_key",
        "spanish_profile_id",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # Compose passes unset optional vars as "" (e.g. ${VAR:-}); treat blank or
        # whitespace-only values as unset so truthiness checks behave correctly.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("spanish_profile_id", mode="after")
    @classmethod
    def _spanish_profile_id_is_uuid(cls, v: str | None) -> str | None:
        # set_spanish_callback parses this as a UUID (the callback's profile_override), so a
        # non-UUID value would raise 500 on every Spanish callback. Fail fast at startup.
        if v is None:
            return None
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("SPANISH_PROFILE_ID must be a valid UUID") from exc
        return v

    @field_validator("compat_default_timezone", mode="after")
    @classmethod
    def _compat_tz_valid(cls, v: str) -> str:
        # Compose may pass COMPAT_DEFAULT_TIMEZONE as "" when unset (${VAR:-}); treat a
        # blank value as the default. A bad IANA name would otherwise raise ValueError
        # inside quiet_hours.next_allowed on every compat call (number-first Contact
        # upsert sets this timezone) — fail fast at startup instead (Constitution III).
        name = v.strip() or "America/New_York"
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("COMPAT_DEFAULT_TIMEZONE must be a valid IANA timezone") from exc
        return name

    @field_validator("livekit_url")
    @classmethod
    def _ws_scheme(cls, v: str) -> str:
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("must be a ws:// or wss:// URL")
        return v

    @field_validator("telnyx_messaging_api_url", "cartesia_api_url")
    @classmethod
    def _https_scheme(cls, v: str) -> str:
        # Both carry a bearer secret in the Authorization header (the Telnyx / Cartesia
        # API key), and the SMS flush also POSTs the rendered body + the contact's phone;
        # a plaintext/hostile endpoint would leak the secret (and PHI). Require https://
        # (operator config).
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

    @field_validator("admin_base_url")
    @classmethod
    def _absolute_base_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("ADMIN_BASE_URL must be absolute (http:// or https://)")
        return v.rstrip("/") if v else v

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

    @property
    def trusted_proxy_set(self) -> frozenset[str]:
        """Parsed TRUSTED_PROXY_IPS — the peer IPs allowed to set X-Forwarded-For.

        Empty (the default) means legacy behavior: trust the XFF first hop
        unconditionally (correct only because Caddy + Cloudflare AOP mTLS front the API
        in prod). See client_ip.client_ip.
        """
        return frozenset(ip.strip() for ip in self.trusted_proxy_ips.split(",") if ip.strip())

    @property
    def compat_webhook_allowed_hosts_set(self) -> frozenset[str]:
        """Hosts attested as in-infrastructure, allowed full-fidelity (PHI) compat webhooks.

        Empty (the default) means NO compat webhook destination may receive a PHI-bearing
        payload (transcript / recording URL / analysis) — the allow-list is layered atop
        the existing SSRF guard (Constitution II, FR-022). Hosts are lowercased for a
        case-insensitive match against the delivery URL's host.
        """
        return frozenset(
            h.strip().lower() for h in self.compat_webhook_allowed_hosts.split(",") if h.strip()
        )

    @property
    def compat_tool_allowed_hosts_set(self) -> frozenset[str]:
        """Hosts a Surface-3 external tool url may POST to (PHI-bearing args egress here).

        Empty (the default) means NO external tool validates — an operator must attest the
        client's edge-function host before external tools can be ingested. Lowercased for a
        case-insensitive match against the tool url's host. Layered atop the SSRF guard the
        execution proxy applies at call time (design §7 step 3).
        """
        return frozenset(
            h.strip().lower() for h in self.compat_tool_allowed_hosts.split(",") if h.strip()
        )

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

    def _db_host_is_local(self) -> bool:
        """True when DATABASE_URL points at loopback/local (dev container, test)."""
        host = (urlsplit(self.database_url.get_secret_value()).hostname or "").lower()
        return host in _LOCAL_HOSTS

    def warn_if_sso_without_hd(self) -> None:
        """Warn (never fail) when SSO is configured without a hosted-domain restriction.

        Without GOOGLE_OAUTH_HD the `hd` claim is not checked, so the admin_users
        allow-list is the only barrier — any personal Google account in the list (or a
        compromised one) can authenticate. Set GOOGLE_OAUTH_HD to the Workspace domain
        in prod for defense-in-depth (oauth.py enforces it when set).
        """
        if self.sso_enabled and not self.google_oauth_hd:
            logger.warning(
                "Google SSO is enabled but GOOGLE_OAUTH_HD is unset; the hosted-domain "
                "(hd) claim is NOT enforced — only the admin_users allow-list gates login. "
                "Set GOOGLE_OAUTH_HD to your Workspace domain for defense-in-depth."
            )

    def warn_if_phi_retention_unbounded(self) -> None:
        """Warn (never fail) when a non-local deployment has no PHI retention window.

        PHI_RETENTION_DAYS=None disables the purge poller, so transcripts and terminal
        dynamic_vars are kept indefinitely. That is fine for local/dev but a HIPAA
        minimization gap for a real deployment — surface it so operators set a window.
        """
        if self.phi_retention_days is None and not self._db_host_is_local():
            logger.warning(
                "PHI_RETENTION_DAYS is unset and DATABASE_URL is non-local; PHI "
                "(transcripts, terminal-call dynamic vars) is retained indefinitely. "
                "Set PHI_RETENTION_DAYS to a HIPAA-compliant window to enable the purge."
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
    settings.warn_if_sso_without_hd()
    settings.warn_if_phi_retention_unbounded()
    return settings
