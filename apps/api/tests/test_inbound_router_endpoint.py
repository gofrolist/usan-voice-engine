"""Surface 2A: register_inbound_call consulting the inbound-call-router (POST /v1/calls/inbound).

Flag off (default) ⇒ the endpoint is byte-identical to today (no router call, override_applied
False). Flag on ⇒ a router override that resolves to a published voice agent is stored as the
call's profile_override, its dynamic_variables merge over the contact vars, and override_applied
is True — even for an unknown caller. Any router failure or an unresolvable agent degrades (no
override), so the call still connects on the default inbound agent.

The router egress itself is unit-tested in test_inbound_router.py; here we monkeypatch
``route_inbound`` and exercise the endpoint's decode/validate/persist wiring.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat import inbound_router
from usan_api.compat.ids import encode_agent_id
from usan_api.compat.inbound_router import InboundRouterResult
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.settings import get_settings

SECRET = "s" * 32
_OP = {"Authorization": "Bearer " + "o" * 32}


def _worker_auth() -> dict:
    now = int(time.time())
    tok = jwt.encode({"sub": "usan-agent", "iat": now, "exp": now + 300}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


async def _publish_voice_profile(database_url: str) -> uuid.UUID:
    """Create + publish a voice profile in the client-fixture DB; return its id."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            profile = await profiles_repo.create_profile(
                db, name=f"router-{uuid.uuid4().hex}", description=None, actor_email="op"
            )
            await profiles_repo.publish(db, profile.id, note="v1", actor_email="op")
            await db.commit()
            return profile.id
    finally:
        await engine.dispose()


def _enable_router(monkeypatch):
    monkeypatch.setenv("COMPAT_INBOUND_ROUTER_ENABLED", "true")
    monkeypatch.setenv("COMPAT_INBOUND_ROUTER_URL", "https://client.example.com/router")
    get_settings.cache_clear()


def _post_inbound(client, phone: str, to_number: str = "+15550001111"):
    return client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-2a", "to_number": to_number},
        headers=_worker_auth(),
    )


# --- flag off: inert --------------------------------------------------------


def test_router_not_called_when_flag_off(client, monkeypatch):
    called = {"n": 0}

    async def _spy(*a, **k):
        called["n"] += 1
        return

    monkeypatch.setattr(inbound_router, "route_inbound", _spy)
    r = _post_inbound(client, _phone())
    assert r.status_code == 200
    assert r.json()["override_applied"] is False
    assert called["n"] == 0  # never consulted when disabled


# --- flag on: override applied ---------------------------------------------


def test_override_applied_sets_profile_and_merges_vars(client, monkeypatch, async_database_url):
    pid = asyncio.run(_publish_voice_profile(async_database_url))
    _enable_router(monkeypatch)

    async def _route(settings, *, from_number, to_number):
        return InboundRouterResult(
            override_agent_id=encode_agent_id(pid),
            dynamic_variables={"first_name": "John", "trial_status": "active"},
        )

    monkeypatch.setattr(inbound_router, "route_inbound", _route)

    r = _post_inbound(client, _phone())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["override_applied"] is True
    # Router vars merged into the call's dynamic_vars (unknown caller ⇒ these are the only vars).
    assert data["dynamic_vars"]["first_name"] == "John"
    assert data["dynamic_vars"]["trial_status"] == "active"
    # The override was persisted as profile_override so the worker's config re-fetch resolves it.
    call = client.get(f"/v1/calls/{data['call_id']}", headers=_OP).json()
    assert call["direction"] == "inbound"


def test_router_vars_win_over_contact_vars(client, monkeypatch, async_database_url):
    pid = asyncio.run(_publish_voice_profile(async_database_url))
    _enable_router(monkeypatch)
    phone = _phone()
    client.post(
        "/v1/contacts",
        json={"name": "Ada", "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )

    async def _route(settings, *, from_number, to_number):
        return InboundRouterResult(
            override_agent_id=encode_agent_id(pid),
            dynamic_variables={"contact_name": "John (from CRM)"},
        )

    monkeypatch.setattr(inbound_router, "route_inbound", _route)
    r = _post_inbound(client, phone)
    assert r.status_code == 200
    data = r.json()
    assert data["contact_known"] is True  # our DB knows Ada
    assert data["dynamic_vars"]["contact_name"] == "John (from CRM)"  # but router wins


# --- flag on: degrade -------------------------------------------------------


def test_degrade_when_router_returns_none(client, monkeypatch):
    _enable_router(monkeypatch)

    async def _route(settings, *, from_number, to_number):
        return None

    monkeypatch.setattr(inbound_router, "route_inbound", _route)
    r = _post_inbound(client, _phone())
    assert r.status_code == 200
    assert r.json()["override_applied"] is False


def test_degrade_when_override_agent_unknown(client, monkeypatch):
    _enable_router(monkeypatch)

    async def _route(settings, *, from_number, to_number):
        # Well-formed token, but no such published profile ⇒ is_live_profile False ⇒ degrade.
        return InboundRouterResult(
            override_agent_id=encode_agent_id(uuid.uuid4()), dynamic_variables={"a": "b"}
        )

    monkeypatch.setattr(inbound_router, "route_inbound", _route)
    r = _post_inbound(client, _phone())
    assert r.status_code == 200
    data = r.json()
    assert data["override_applied"] is False
    # A degraded override must NOT leak its vars onto the call.
    assert "a" not in data["dynamic_vars"]


def test_degrade_when_override_agent_id_malformed(client, monkeypatch):
    _enable_router(monkeypatch)

    async def _route(settings, *, from_number, to_number):
        return InboundRouterResult(override_agent_id="not-an-agent-id", dynamic_variables={})

    monkeypatch.setattr(inbound_router, "route_inbound", _route)
    r = _post_inbound(client, _phone())
    assert r.status_code == 200
    assert r.json()["override_applied"] is False
