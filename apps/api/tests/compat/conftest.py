"""Shared fixtures for the compat test sub-package.

The surface-coverage test (test_surface_coverage.py) only inspects route tables — it
never touches a real database.  This conftest provides the minimum Settings env-vars so
``get_settings()`` does not raise when called without the top-level conftest's
``database_url`` / ``client`` fixtures.
"""

from __future__ import annotations

import pytest

from usan_api.settings import get_settings


@pytest.fixture(autouse=True)
def _compat_minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum required env-vars for Settings validation.

    Values are obviously fake — they are only used for route-inspection tests that
    never connect to any external service.  Tests that need a real DB must use the
    top-level ``client`` / ``compat_client`` fixtures instead, which supply a live
    Postgres URL and override these.
    """
    # Clear the lru_cache first so the new env values are picked up even if another
    # test in a different module already populated the cache with different values.
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://usan:usan@localhost:5432/usan")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
