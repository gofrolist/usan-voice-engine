import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from pydantic import BaseModel

from usan_api import background, retry_orchestrator
from usan_api.db.session import dispose_engine
from usan_api.logging_config import configure_logging
from usan_api.routers import calls, dnc, elders, tools, webhooks
from usan_api.settings import get_settings


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = asyncio.Event()
    poller_task: asyncio.Task[None] | None = None
    if settings.retry_poller_enabled:
        poller_task = asyncio.create_task(retry_orchestrator.run_poller(settings, stop))
    try:
        yield
    finally:
        stop.set()
        if poller_task is not None:
            poller_task.cancel()
            with suppress(asyncio.CancelledError):
                await poller_task
        # Drain longer than the longest blocking dial (ringing timeout) so an
        # in-flight dial finishes and writes its outcome before the engine closes;
        # otherwise it would write against a disposed engine and stick at 'dialing'.
        drain_timeout = float(settings.outbound_ringing_timeout_s) + 15.0
        await background.drain(timeout=drain_timeout)
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="USAN Voice Engine API", version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    app.include_router(tools.router)

    return app
