import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from loguru import logger
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint

from usan_api import (
    background,
    retention,
    retry_orchestrator,
    schedule_orchestrator,
    webhook_delivery,
)
from usan_api.db.session import dispose_engine, get_session_factory
from usan_api.logging_config import configure_logging
from usan_api.observability.instrumentation import setup_metrics
from usan_api.ratelimit import OperatorRateLimitMiddleware
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.repositories import custom_variables as custom_variables_repo
from usan_api.routers import (
    admin_audit,
    admin_calls,
    admin_custom_variables,
    admin_defaults,
    admin_elders,
    admin_profiles,
    admin_tool_catalog,
    admin_tools,
    admin_users,
    admin_variable_catalog,
    auth,
    batches,
    calls,
    dnc,
    elders,
    runtime,
    schedules,
    tools,
    webhook_endpoints,
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


async def _check_contact_name_shadow() -> None:
    """Deploy-time guard (US4 / FR-024): warn (name-only) if a pre-existing custom
    ``contact_name`` row is shadowed by the new builtin alias. Best-effort — a DB
    hiccup logs and never crashes startup."""
    try:
        async with get_session_factory()() as db:
            await custom_variables_repo.warn_if_contact_name_custom_exists(db)
    except Exception:  # noqa: BLE001 - startup must not crash on a check failure
        logger.exception("Failed to run the contact_name shadow check")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await _seed_admin_allowlist(settings)
    await _check_contact_name_shadow()
    stop = asyncio.Event()
    poller_tasks: list[asyncio.Task[None]] = []
    if settings.retry_poller_enabled:
        poller_tasks.append(asyncio.create_task(retry_orchestrator.run_poller(settings, stop)))
    if settings.phi_retention_days is not None:
        poller_tasks.append(asyncio.create_task(retention.run_poller(settings, stop)))
    if settings.scheduler_poller_enabled:
        poller_tasks.append(asyncio.create_task(schedule_orchestrator.run_poller(settings, stop)))
    # The webhook delivery poller ALWAYS starts (spec §5.1): WEBHOOK_DELIVERY_ENABLED
    # gates only the claim+POST half of each cycle; the housekeeping half (crash-residue
    # sweep, 7-day expiry, 30-day prune) and the pending-depth gauge must run even with
    # the flag off so a flag-off backlog is swept, expired, and visible.
    # Test-suite note: every `client`-fixture test therefore runs this poller live.
    # That is safe because (a) flag-off means the delivery half never claims rows, and
    # (b) the task holds THIS lifespan-time Settings snapshot — a per-test
    # setenv("WEBHOOK_DELIVERY_ENABLED", "true") flips per-request settings but never
    # the running poller, so enqueued ping rows stay deterministically 'pending'. Its
    # first cycle runs real housekeeping SQL against the shared test DB; harmless,
    # since sweep/expire/prune only touch rows older than their thresholds.
    poller_tasks.append(asyncio.create_task(webhook_delivery.run_poller(settings, stop)))
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

    @app.middleware("http")
    async def _admin_no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Transcript JSON and live bearer recording URLs must never be written to a
        # shared workstation's HTTP cache (spec §8). Neither the API nor Caddy sets
        # cache headers otherwise; scoped to the admin plane only.
        response = await call_next(request)
        if request.url.path.startswith("/v1/admin/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(admin_profiles.router)
    app.include_router(admin_defaults.router)
    app.include_router(admin_users.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_elders.router)
    app.include_router(admin_variable_catalog.router)
    app.include_router(admin_custom_variables.router)
    app.include_router(admin_tool_catalog.router)
    app.include_router(admin_tools.router)
    app.include_router(admin_calls.router)
    app.include_router(auth.router)
    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(schedules.router)
    app.include_router(batches.router)
    app.include_router(webhook_endpoints.router)
    app.include_router(webhook_endpoints.deliveries_router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    app.include_router(tools.router)
    app.include_router(runtime.router)

    setup_metrics(app)

    return app
