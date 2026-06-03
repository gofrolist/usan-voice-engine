import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import cast

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from usan_api import background, retention, retry_orchestrator
from usan_api.db.session import dispose_engine
from usan_api.logging_config import configure_logging
from usan_api.ratelimit import limiter
from usan_api.routers import calls, dnc, elders, tools, webhooks
from usan_api.settings import Settings, get_settings


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = asyncio.Event()
    poller_tasks: list[asyncio.Task[None]] = []
    if settings.retry_poller_enabled:
        poller_tasks.append(asyncio.create_task(retry_orchestrator.run_poller(settings, stop)))
    if settings.phi_retention_days is not None:
        poller_tasks.append(asyncio.create_task(retention.run_poller(settings, stop)))
    try:
        yield
    finally:
        stop.set()
        for task in poller_tasks:
            task.cancel()
        for task in poller_tasks:
            with suppress(asyncio.CancelledError):
                await task
        # Drain longer than the longest blocking dial (ringing timeout) so an
        # in-flight dial finishes and writes its outcome before the engine closes;
        # otherwise it would write against a disposed engine and stick at 'dialing'.
        drain_timeout = float(settings.outbound_ringing_timeout_s) + 15.0
        await background.drain(timeout=drain_timeout)
        await dispose_engine()


def _install_rate_limiting(app: FastAPI, settings: Settings) -> None:
    """Wire the shared limiter onto the app.

    Only the operator routes are decorated with ``limiter.limit`` (see
    usan_api.ratelimit) — internal service/webhook/health routes are never
    throttled. When RATE_LIMIT_ENABLED is false the limiter passes through
    without counting, so the decorators become no-ops.
    """
    limiter.enabled = settings.rate_limit_enabled
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    # slowapi's handler signature is narrower than Starlette's generic Exception
    # handler type; the cast keeps the registration type-clean.
    handler = cast("Callable[[Request, Exception], Response]", _rate_limit_exceeded_handler)
    app.add_exception_handler(RateLimitExceeded, handler)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Secure by default: no OpenAPI schema or docs UIs unless DOCS_ENABLED is set.
    docs_url = "/docs" if settings.docs_enabled else None
    redoc_url = "/redoc" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None

    app = FastAPI(
        title="USAN Voice Engine API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    _install_rate_limiting(app, settings)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    app.include_router(tools.router)

    return app
