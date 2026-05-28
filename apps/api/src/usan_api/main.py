from fastapi import FastAPI
from pydantic import BaseModel

from usan_api.logging_config import configure_logging
from usan_api.routers import dnc
from usan_api.settings import get_settings


class HealthResponse(BaseModel):
    status: str


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="USAN Voice Engine API", version="0.1.0")

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(dnc.router)

    return app
