"""T045 — the compat OpenAPI/docs are fully separate from the native API and gated by
``COMPAT_DOCS_ENABLED`` (feature 003, US-cross-cutting / SC-007 isolation).

The compat sub-app carries its OWN OpenAPI document under ``/compat/*`` — never the
native ``/docs`` / ``/openapi.json`` — and serves it only when the toggle is on. When
off (the default), the sub-app exposes no schema/docs surface at all.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from usan_api.compat.app import build_compat_app
from usan_api.settings import get_settings

_COMPAT_TITLE = "USAN Voice Engine - RetellAI-Compatible API"


def test_compat_docs_disabled_by_default(bare_client):
    # Default test env: COMPAT_DOCS_ENABLED is off, so the mounted sub-app serves no
    # OpenAPI/docs/redoc surface (openapi_url/docs_url are None -> no such route).
    assert bare_client.get("/compat/openapi.json").status_code == 404
    assert bare_client.get("/compat/docs").status_code == 404
    assert bare_client.get("/compat/redoc").status_code == 404


def test_compat_docs_enabled_serves_a_separate_self_contained_openapi(bare_client):
    # Build a docs-ENABLED sub-app from the (valid, env-backed) test settings and assert
    # its OpenAPI is the compat document — self-contained, with zero native /v1 paths.
    settings = get_settings().model_copy(update={"compat_docs_enabled": True})
    with TestClient(build_compat_app(settings)) as client:
        r = client.get("/compat/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == _COMPAT_TITLE
        paths = schema["paths"]
        # Compat endpoints ARE documented...
        assert "/create-agent" in paths
        assert "/list-voices" in paths
        assert "/create-batch-call" in paths
        # ...and not a single native /v1 path leaks into the compat document.
        assert not any(p.startswith("/v1/") for p in paths)


def test_native_openapi_excludes_compat_paths(bare_client):
    # The native app documents only native routes; the mounted sub-app's RetellAI paths
    # never appear in the native schema (when the native docs are served at all).
    r = bare_client.get("/openapi.json")
    if r.status_code != 200:
        return  # native docs disabled in this env — nothing to assert
    paths = r.json().get("paths", {})
    assert "/create-agent" not in paths
    assert "/v2/create-phone-call" not in paths
    assert "/list-voices" not in paths
