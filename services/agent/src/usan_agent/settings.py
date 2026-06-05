from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts where plaintext http:// is expected: the API runs on the Docker bridge in
# prod, reachable as http://api:8000 with no transport TLS. Any other host over
# plaintext http risks PHI traversing an untrusted network, so we reject it at startup.
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "api"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    livekit_api_key: str = Field(..., min_length=1, alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(..., min_length=32, alias="LIVEKIT_API_SECRET")
    livekit_url: str = Field(..., min_length=1, alias="LIVEKIT_URL")
    cartesia_api_key: str = Field(..., min_length=1, alias="CARTESIA_API_KEY")
    # LLM runs on Vertex AI (HIPAA-BAA-covered), not the Gemini Developer API. The
    # plugin authenticates via ADC (the attached VM service account), so there is no
    # LLM API key — only the GCP project + location. Default "global": gemini-3.1-flash-lite
    # is served on the global endpoint, not regional us-east1. See Plan 4e Task A1.
    gcp_project: str = Field(..., min_length=1, alias="GCP_PROJECT")
    vertex_location: str = Field(default="global", alias="VERTEX_LOCATION")
    default_cartesia_voice_id: str = Field(..., min_length=1, alias="DEFAULT_CARTESIA_VOICE_ID")
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
    api_base_url: str = Field(..., min_length=1, alias="API_BASE_URL")
    jwt_signing_key: str = Field(..., min_length=32, alias="JWT_SIGNING_KEY")
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
    outbound_answer_timeout_s: float = Field(
        default=50.0, ge=5.0, le=180.0, alias="OUTBOUND_ANSWER_TIMEOUT_S"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    @field_validator("livekit_url")
    @classmethod
    def _ws_scheme(cls, v: str) -> str:
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("must be a ws:// or wss:// URL")
        return v

    @field_validator("api_base_url")
    @classmethod
    def _http_scheme(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("must be an http:// or https:// URL")
        if parsed.scheme == "http" and (parsed.hostname or "") not in _LOCAL_HTTP_HOSTS:
            # PHI (transcripts, check-in results, call metadata) must not cross an
            # untrusted network in the clear. The Docker-bridge default is allowlisted;
            # any other plaintext-http host is a misconfiguration — fail closed.
            raise ValueError(
                "API_BASE_URL uses plaintext http with a non-local host; "
                "PHI must travel over https or an internal-only network"
            )
        return v


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
        return Settings()
    except ValidationError as e:
        # Re-raise as ValueError naming the offending env vars, so the message
        # is stable and callers don't depend on Pydantic's internal wording.
        missing = _field_names(e.errors(), error_type="missing")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}") from e
        invalid = _field_names(e.errors())
        raise ValueError(f"Invalid configuration for: {', '.join(invalid)}") from e
