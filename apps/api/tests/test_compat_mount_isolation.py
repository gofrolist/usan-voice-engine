"""Foundational mount-isolation tests (T005): the native /v1 plane + /health are unchanged
after the compat sub-app is mounted, compat errors use the RetellAI {status,message}
envelope while native errors keep {detail}, and the startup path-collision assertion fires
on a shadowed path."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from usan_api.main import _assert_no_route_collisions


def test_health_unaffected_by_mount(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_native_route_keeps_detail_envelope(bare_client):
    # An existing native /v1 route called without a session still returns the native
    # {"detail": ...} error shape — the compat envelope never bleeds onto /v1 (SC-007).
    r = bare_client.get("/v1/admin/contacts")
    assert r.status_code == 401
    body = r.json()
    assert "detail" in body
    assert "status" not in body
    assert "message" not in body


def test_compat_unmatched_path_uses_status_message_envelope(client):
    # A path only the mounted compat sub-app can serve: an unknown one yields the RetellAI
    # {status,message} envelope (rendered by the compat handlers), not native {detail}.
    r = client.get("/this-is-not-a-real-endpoint-xyz")
    assert r.status_code == 404
    body = r.json()
    assert body.get("status") == 404
    assert "message" in body
    assert "detail" not in body


# Build the probe apps with docs disabled so they carry ONLY the routes added below
# (a default FastAPI() also registers /docs + /openapi.json, which would themselves clash).
def _bare_app() -> FastAPI:
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def test_collision_assertion_fires_on_shadowed_path():
    native = _bare_app()

    @native.get("/v2/clash")
    async def _native_clash() -> dict[str, str]:
        return {}

    compat = _bare_app()

    @compat.get("/v2/clash")
    async def _compat_clash() -> dict[str, str]:
        return {}

    with pytest.raises(RuntimeError, match="shadow"):
        _assert_no_route_collisions(native, compat)


def test_collision_assertion_passes_when_disjoint():
    native = _bare_app()

    @native.get("/v1/thing")
    async def _native_thing() -> dict[str, str]:
        return {}

    compat = _bare_app()

    @compat.get("/v2/other")
    async def _compat_other() -> dict[str, str]:
        return {}

    _assert_no_route_collisions(native, compat)  # must not raise
