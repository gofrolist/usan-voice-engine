"""T071 (US8): set_spanish_callback tool contract.

When the elder speaks Spanish, the call-scoped ``set_spanish_callback`` records the
language preference on the elder (``meta['language'] = 'es'``) and creates a callback
request flagged for Spanish (FR-040), so the callback dialer reaches them back in
Spanish. When ``SPANISH_PROFILE_ID`` is configured the callback carries that profile as
its ``profile_override``. Token-scope and missing-elder guards mirror the other tools.
"""

import asyncio
import time
import uuid

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.models import CallbackRequest, Elder
from usan_api.settings import get_settings

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _worker_token(secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


def _create_elder(client, phone: str) -> str:
    r = client.post(
        "/v1/elders",
        json={"name": "Ada", "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> dict:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"sp-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()


def _query(url, coro_factory):
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                return await coro_factory(db)
        finally:
            await engine.dispose()

    return asyncio.run(_do())


def _elder(url, elder_id: str) -> Elder:
    return _query(url, lambda db: db.get(Elder, uuid.UUID(elder_id)))


def _callbacks(url, elder_id: str) -> list[CallbackRequest]:
    async def _q(db):
        rows = (
            await db.execute(
                select(CallbackRequest).where(CallbackRequest.elder_id == uuid.UUID(elder_id))
            )
        ).scalars()
        return list(rows)

    return _query(url, _q)


def _create_profile(url) -> str:
    from usan_api.repositories import agent_profiles as profiles_repo

    async def _q(db):
        profile = await profiles_repo.create_profile(
            db, name=f"es-{uuid.uuid4().hex}", description=None, actor_email="t@usan.test"
        )
        await db.commit()
        return str(profile.id)

    return _query(url, _q)


def test_set_spanish_callback_sets_language_and_creates_callback(
    client, mock_dispatch, async_database_url
):
    phone = _phone()
    elder_id = _create_elder(client, phone)
    call_id = _enqueue(client, elder_id)["id"]

    r = client.post(
        "/v1/tools/set_spanish_callback", json={"call_id": call_id}, headers=_auth(call_id)
    )
    assert r.status_code == 200, r.text

    # Language preference recorded so future calls/callbacks know to use Spanish.
    elder = _elder(async_database_url, elder_id)
    assert elder.meta.get("language") == "es"

    # A dial-able Spanish callback was created (requested_at set => the dialer picks it up).
    callbacks = _callbacks(async_database_url, elder_id)
    assert len(callbacks) == 1
    cb = callbacks[0]
    assert cb.status == "open"
    assert cb.requested_at is not None
    assert "spanish" in cb.requested_time_text.lower()
    assert cb.profile_override is None  # no SPANISH_PROFILE_ID configured by default


def test_set_spanish_callback_uses_configured_profile(
    client, mock_dispatch, async_database_url, monkeypatch
):
    profile_id = _create_profile(async_database_url)
    monkeypatch.setenv("SPANISH_PROFILE_ID", profile_id)
    get_settings.cache_clear()

    phone = _phone()
    elder_id = _create_elder(client, phone)
    call_id = _enqueue(client, elder_id)["id"]

    r = client.post(
        "/v1/tools/set_spanish_callback", json={"call_id": call_id}, headers=_auth(call_id)
    )
    assert r.status_code == 200, r.text

    cb = _callbacks(async_database_url, elder_id)[0]
    assert str(cb.profile_override) == profile_id


def test_set_spanish_callback_rejects_wrong_call_token(client, mock_dispatch, async_database_url):
    phone = _phone()
    elder_id = _create_elder(client, phone)
    call_id = _enqueue(client, elder_id)["id"]
    r = client.post(
        "/v1/tools/set_spanish_callback",
        json={"call_id": call_id},
        headers=_auth(str(uuid.uuid4())),  # token scoped to a different call
    )
    assert r.status_code == 403


def test_set_spanish_callback_409_when_call_has_no_elder(client, mock_dispatch):
    inbound = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19990007777", "livekit_room": f"sp-{uuid.uuid4()}"},
        headers={"Authorization": f"Bearer {_worker_token()}"},
    ).json()
    call_id = inbound["call_id"]
    r = client.post(
        "/v1/tools/set_spanish_callback", json={"call_id": call_id}, headers=_auth(call_id)
    )
    assert r.status_code == 409
