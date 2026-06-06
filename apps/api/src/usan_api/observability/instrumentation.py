"""Prometheus instrumentation wiring for the FastAPI app.

A single process-wide Instrumentator. prometheus_client metrics live in one
global registry per process, so the built-in RED collectors must be created
exactly once. Reusing this instance across create_app() calls (the test suite
rebuilds the app per test) adds the middleware + /metrics route to each app
WITHOUT re-registering collectors — which would raise
"Duplicated timeseries in CollectorRegistry".
"""

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

# Importing the custom metrics module here guarantees the three counters are
# registered on the default registry whenever /metrics is wired, so their
# HELP/TYPE lines appear even before the first increment.
from usan_api.observability import custom_metrics  # noqa: F401

_INSTRUMENTATOR = Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics", "/health"],
)


def setup_metrics(app: FastAPI) -> None:
    """Instrument `app` and expose GET /metrics (internal scrape endpoint)."""
    _INSTRUMENTATOR.instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False, should_gzip=False
    )
