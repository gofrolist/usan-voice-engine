"""Build the mounted RetellAI-compatible sub-application (feature 003).

Starlette does not share exception handlers, OpenAPI, or app-level dependencies across a
mount, so the compat surface is its OWN FastAPI app: the RetellAI ``{status,message}`` error
envelope, a separate (toggleable) OpenAPI/docs, and a uniform Bearer-key auth baseline, all
isolated from the native ``/v1`` ``{detail}`` plane (FR-004 / SC-007).

Business routers (calls, agents, retell_llm, batches, catalog, unsupported) are registered
by their owning user-story tasks; Foundational ships the shell + auth + error handlers.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import register_exception_handlers
from usan_api.compat.routers import agents as compat_agents
from usan_api.compat.routers import batches as compat_batches
from usan_api.compat.routers import calls as compat_calls
from usan_api.compat.routers import catalog as compat_catalog
from usan_api.compat.routers import retell_llm as compat_retell_llm
from usan_api.compat.routers import unsupported as compat_unsupported
from usan_api.settings import Settings


def build_compat_app(settings: Settings) -> FastAPI:
    # Distinct docs paths (under /compat/) so the RetellAI OpenAPI never collides with the
    # native /docs + /openapi.json and is toggled independently (COMPAT_DOCS_ENABLED).
    docs_url = "/compat/docs" if settings.compat_docs_enabled else None
    redoc_url = "/compat/redoc" if settings.compat_docs_enabled else None
    openapi_url = "/compat/openapi.json" if settings.compat_docs_enabled else None

    app = FastAPI(
        title="USAN Voice Engine - RetellAI-Compatible API",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        # FastAPI's Swagger oauth2-redirect defaults to /docs/oauth2-redirect regardless of
        # docs_url; pin it under /compat so it can't collide with the native one if BOTH docs
        # surfaces are ever enabled (the startup collision assert would otherwise fire).
        swagger_ui_oauth2_redirect_url=(
            "/compat/docs/oauth2-redirect" if settings.compat_docs_enabled else None
        ),
        # Every compat route is auth-gated: this app-level dependency guarantees a Bearer
        # compat key even on a route that forgets to declare it. FastAPI dedups it with the
        # per-route Depends(get_compat_db) handlers use for the session (one execution).
        dependencies=[Depends(get_compat_db)],
    )
    register_exception_handlers(app)
    app.include_router(compat_calls.router)
    app.include_router(compat_agents.router)
    app.include_router(compat_retell_llm.router)
    app.include_router(compat_batches.router)
    app.include_router(compat_catalog.router)
    app.include_router(compat_unsupported.router)
    return app
