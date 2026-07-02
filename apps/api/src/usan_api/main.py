import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text
from starlette.middleware.base import RequestResponseEndpoint

from usan_api import (
    background,
    callback_dialer,
    family_report_job,
    notification_outbox,
    retention,
    retry_orchestrator,
    schedule_orchestrator,
    webhook_delivery,
)
from usan_api.compat import kb_ingestion_poller
from usan_api.compat import webhook_delivery as compat_webhook_delivery
from usan_api.compat.app import build_compat_app
from usan_api.db.session import dispose_engine, get_session_factory
from usan_api.logging_config import configure_logging
from usan_api.observability.instrumentation import setup_metrics
from usan_api.ratelimit import OperatorRateLimitMiddleware
from usan_api.repositories import admin_users as admin_users_repo
from usan_api.repositories import custom_variables as custom_variables_repo
from usan_api.routers import (
    admin_audit,
    admin_calls,
    admin_compat_keys,
    admin_contacts,
    admin_custom_variables,
    admin_defaults,
    admin_dnc,
    admin_family,
    admin_invites,
    admin_knowledge_bases,
    admin_members,
    admin_model_catalog,
    admin_organizations,
    admin_profile_tests,
    admin_profiles,
    admin_schedules,
    admin_tool_catalog,
    admin_tools,
    admin_variable_catalog,
    admin_voice_catalog,
    auth,
    batches,
    calls,
    contacts,
    dnc,
    runtime,
    schedules,
    tools,
    webhook_endpoints,
    webhooks,
)
from usan_api.settings import Settings, get_settings


class HealthResponse(BaseModel):
    status: str
    version: str
    git_sha: str


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


async def _check_rls_role_capability() -> None:
    """Deploy-time guard (P1 tenancy): warn LOUDLY if the app's DB role BYPASSES Row-Level
    Security — tenant isolation is silently void if the role is a superuser or has
    BYPASSRLS (e.g. prod DATABASE_URL still points at ``usan`` / cloudsqlsuperuser instead
    of the non-superuser ``usan_app``). Best-effort; logs CRITICAL, never crashes startup.
    Intentionally a log, not a hard failure: P1 ships behavior-preserving with prod still
    on ``usan`` until the DATABASE_URL cutover, so a raise would break the deploy."""
    try:
        async with get_session_factory()() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
        if row[1] or row[2]:
            logger.critical(
                "DB role {u} BYPASSES Row-Level Security (rolsuper={s}, rolbypassrls={b}) — "
                "tenant isolation is NOT enforced. Point the API DATABASE_URL at the "
                "non-superuser usan_app role.",
                u=row[0],
                s=row[1],
                b=row[2],
            )
    except Exception:  # noqa: BLE001 - startup must not crash on a check failure
        logger.exception("Failed to run the RLS role-capability check")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await _seed_admin_allowlist(settings)
    await _check_contact_name_shadow()
    await _check_rls_role_capability()
    stop = asyncio.Event()
    poller_tasks: list[asyncio.Task[None]] = []
    if settings.retry_poller_enabled:
        poller_tasks.append(asyncio.create_task(retry_orchestrator.run_poller(settings, stop)))
    if settings.phi_retention_days is not None:
        poller_tasks.append(asyncio.create_task(retention.run_poller(settings, stop)))
    if settings.scheduler_poller_enabled:
        poller_tasks.append(asyncio.create_task(schedule_orchestrator.run_poller(settings, stop)))
    if settings.kb_ingestion_poller_enabled:
        poller_tasks.append(asyncio.create_task(kb_ingestion_poller.run_poller(settings, stop)))
    # Notification outbox (Clara Care Parity 002): delivers family alerts / reports /
    # opt-out acks (sms_messages with call_id IS NULL). Ship-inert — off by default.
    if settings.notification_outbox_enabled:
        poller_tasks.append(asyncio.create_task(notification_outbox.run_poller(settings, stop)))
    # Callback auto-dial (US8): materialize due callback requests into outbound calls.
    if settings.callback_dialer_poller_enabled:
        poller_tasks.append(asyncio.create_task(callback_dialer.run_poller(settings, stop)))
    # Monthly family report (US8): aggregate the prior month + send the PHI-minimized SMS.
    if settings.family_report_poller_enabled:
        poller_tasks.append(asyncio.create_task(family_report_job.run_poller(settings, stop)))
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
    # Compat (RetellAI) webhook delivery poller (feature 003 / US2). Like the native poller
    # above it ALWAYS starts: COMPAT_WEBHOOK_DELIVERY_ENABLED gates only the claim+POST half,
    # so housekeeping (sweep/expire/prune) + the backlog gauge run flag-independently.
    poller_tasks.append(asyncio.create_task(compat_webhook_delivery.run_poller(settings, stop)))
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
        auth_limit=settings.rate_limit_auth,
        compat_limit=settings.compat_key_rate_limit,
        trusted_proxies=settings.trusted_proxy_set,
    )


def _assert_no_route_collisions(app: FastAPI, compat_app: FastAPI) -> None:
    """Fail fast if a compat route's path exactly shadows a native one (it would be
    unreachable behind the root mount). The native /v1 plane vs the RetellAI unversioned +
    /v2 + /v3 paths are disjoint today; this guards against a future accidental overlap."""

    def _paths(a: FastAPI) -> set[str]:
        return {p for r in a.routes if isinstance(p := getattr(r, "path", None), str)}

    clash = {p for p in _paths(app) & _paths(compat_app) if p != "/"}
    if clash:
        raise RuntimeError(f"compat routes shadow native paths: {sorted(clash)}")


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
        # `settings` is captured from the create_app closure — no DB, no auth, so the
        # Docker/Caddy health probes stay dependency-free while also reporting build info.
        return HealthResponse(status="ok", version=settings.app_version, git_sha=settings.git_sha)

    app.include_router(admin_profiles.router)
    app.include_router(admin_defaults.router)
    app.include_router(admin_profile_tests.router)
    app.include_router(admin_members.router)
    app.include_router(admin_organizations.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_contacts.router)
    app.include_router(admin_knowledge_bases.router)
    app.include_router(admin_schedules.router)
    app.include_router(admin_family.router)
    app.include_router(admin_invites.router)
    app.include_router(admin_variable_catalog.router)
    app.include_router(admin_custom_variables.router)
    app.include_router(admin_tool_catalog.router)
    app.include_router(admin_voice_catalog.router)
    app.include_router(admin_model_catalog.router)
    app.include_router(admin_tools.router)
    app.include_router(admin_calls.router)
    app.include_router(admin_dnc.router)
    app.include_router(admin_compat_keys.router)
    app.include_router(auth.router)
    app.include_router(contacts.router)
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

    # Mount the RetellAI-compatible sub-app LAST so every native /v1 route + /health +
    # /metrics is matched first; the root mount catches only the (otherwise unmatched)
    # RetellAI paths. Assert no compat route exactly shadows a native path so a future
    # overlap fails fast at startup instead of silently becoming unreachable.
    compat_app = build_compat_app(settings)
    _assert_no_route_collisions(app, compat_app)
    app.mount("/", compat_app)

    return app
