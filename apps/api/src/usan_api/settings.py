from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = Field(..., min_length=1, alias="DATABASE_URL")
    livekit_api_key: str = Field(..., min_length=1, alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(..., min_length=32, alias="LIVEKIT_API_SECRET")
    livekit_url: str = Field(..., min_length=1, alias="LIVEKIT_URL")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        # Re-raise as ValueError so callers don't need to import pydantic
        missing = [err["loc"][0] for err in e.errors() if err["type"] == "missing"]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}") from e
        raise ValueError(str(e)) from e
