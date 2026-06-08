import asyncio
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as repo

_SECRET = "s" * 32


def _worker_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, _SECRET, algorithm="HS256"
    )


def _wauth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_worker_token()}"}


async def _seed_default(async_url: str, *, direction: str, voice_id: str) -> str:
    engine = create_async_engine(async_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            profile = await repo.create_profile(
                db, name=f"p-{uuid.uuid4().hex}", description=None, actor_email="op"
            )
            cfg = dict(profile.draft_config)
            cfg["voice"] = {**cfg["voice"], "cartesia_voice_id": voice_id}
            await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
            await repo.publish(db, profile.id, note="v1", actor_email="op")
            await repo.set_default(db, profile.id, direction=direction)
            await db.commit()
            return str(profile.id)
    finally:
        await engine.dispose()


def test_agent_config_requires_token(client):
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"})
    assert r.status_code == 401


def test_agent_config_invalid_direction_422(client):
    r = client.get("/v1/runtime/agent-config", params={"direction": "sideways"}, headers=_wauth())
    assert r.status_code == 422


def test_agent_config_missing_direction_422(client):
    r = client.get("/v1/runtime/agent-config", headers=_wauth())
    assert r.status_code == 422


def test_agent_config_default_when_nothing_configured(client):
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"}, headers=_wauth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "default"
    assert body["profile_id"] is None
    assert body["config"]["prompts"]["system_prompt"].startswith("You are a warm")
    assert body["config"]["tools"]["enabled"] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "end_call",
    ]


def test_agent_config_resolves_published_default(client, async_database_url):
    pid = asyncio.run(_seed_default(async_database_url, direction="outbound", voice_id="voice-XYZ"))
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"}, headers=_wauth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["profile_id"] == pid
    assert body["version"] == 1
    assert body["config"]["voice"]["cartesia_voice_id"] == "voice-XYZ"


def test_agent_config_unknown_call_id_falls_back_to_default(client):
    # A call_id that does not exist must not 404 — resolution proceeds on direction only.
    r = client.get(
        "/v1/runtime/agent-config",
        params={"direction": "outbound", "call_id": str(uuid.uuid4())},
        headers=_wauth(),
    )
    assert r.status_code == 200
    assert r.json()["source"] == "default"


def test_agent_config_not_rate_limited(client, async_database_url):
    # Runtime route must not be throttled by the operator rate limiter.
    asyncio.run(_seed_default(async_database_url, direction="inbound", voice_id="vin"))
    for _ in range(20):
        r = client.get(
            "/v1/runtime/agent-config", params={"direction": "inbound"}, headers=_wauth()
        )
        assert r.status_code == 200
