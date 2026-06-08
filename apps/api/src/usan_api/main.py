import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel

from usan_api import background, retention, retry_orchestrator
from usan_api.db.session import dispose_engine, get_session_factory
from usan_api.logging_config import configure_logging
from usan_api.observability.instrumentation import setup_metrics
from usan_api.ratelimit import OperatorRateLimitMiddleware
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.routers import (
    admin_audit,
    admin_elders,
    admin_profiles,
    admin_users,
    auth,
    calls,
    dnc,
    elders,
    runtime,
    tools,
    webhooks,
)
from usan_api.settings import Settings, get_settings


class HealthResponse(BaseModel):
    status: str


async def _seed_admin_allowlist(settings: Settings) -> None:
    """Insert ADMIN_BOOTSTRAP_EMAILS into admin_users on startup (idempotent).

    Best-effort: a transient DB hiccup logs and does not crash the API (health stays
    up). Without at least one allow-listed email, nobody can log in via SSO.
    """
    emails = settings.bootstrap_emails_list
    if not emails:
        return
    try:
        async with get_session_factory()() as db:
            n = await admin_users_repo.seed_bootstrap(db, emails)
            await db.commit()
        if n:
            logger.info("Seeded {n} bootstrap admin user(s)", n=n)
    except Exception:  # noqa: BLE001 - startup must not crash on a seeding failure
        logger.exception("Failed to seed bootstrap admin allow-list")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await _seed_admin_allowlist(settings)
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
    """Throttle the operator/management plane per client, before authentication.

    Internal service/webhook/health routes are never matched (see
    usan_api.ratelimit). A no-op when RATE_LIMIT_ENABLED is false.
    """
    app.add_middleware(
        OperatorRateLimitMiddleware,
        limit=settings.rate_limit_default,
        enabled=settings.rate_limit_enabled,
    )


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

    app.include_router(admin_profiles.router)
    app.include_router(admin_users.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_elders.router)
    app.include_router(auth.router)
    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    app.include_router(tools.router)
    app.include_router(runtime.router)

    setup_metrics(app)

    return app
