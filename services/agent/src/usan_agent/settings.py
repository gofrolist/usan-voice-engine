from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    livekit_api_key: str = Field(..., min_length=1, alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(..., min_length=32, alias="LIVEKIT_API_SECRET")
    livekit_url: str = Field(..., min_length=1, alias="LIVEKIT_URL")
    cartesia_api_key: str = Field(..., min_length=1, alias="CARTESIA_API_KEY")
    gemini_api_key: str = Field(..., min_length=1, alias="GEMINI_API_KEY")
    default_cartesia_voice_id: str = Field(..., min_length=1, alias="DEFAULT_CARTESIA_VOICE_ID")
    agent_name: str = Field(default="usan-agent", alias="AGENT_NAME")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    @field_validator("livekit_url")
    @classmethod
    def _ws_scheme(cls, v: str) -> str:
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("must be a ws:// or wss:// URL")
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
