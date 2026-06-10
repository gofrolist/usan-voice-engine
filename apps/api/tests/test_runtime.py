import asyncio
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
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


async def _publish_profile(db, *, voice_id: str) -> uuid.UUID:
    """Create a profile, set a distinctive voice id, publish it. Returns the id."""
    profile = await repo.create_profile(
        db, name=f"p-{uuid.uuid4().hex}", description=None, actor_email="op"
    )
    cfg = dict(profile.draft_config)
    cfg["voice"] = {**cfg["voice"], "cartesia_voice_id": voice_id}
    await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
    await repo.publish(db, profile.id, note="v1", actor_email="op")
    return profile.id


async def _seed_call_with_elder(
    async_url: str,
    *,
    elder_voice_id: str,
    override_voice_id: str | None = None,
) -> tuple[str, str, str | None]:
    """Seed a published elder profile P, an Elder pointing at P, and an outbound Call.

    Returns (call_id, elder_profile_id, override_profile_id). When override_voice_id is
    given, a second published profile is created and set as the Call.profile_override.
    """
    engine = create_async_engine(async_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            elder_pid = await _publish_profile(db, voice_id=elder_voice_id)
            override_pid: uuid.UUID | None = None
            if override_voice_id is not None:
                override_pid = await _publish_profile(db, voice_id=override_voice_id)
            elder = Elder(
                name="Ada",
                phone_e164=f"+1555{uuid.uuid4().int % 10**7:07d}",
                timezone="America/New_York",
                agent_profile_id=elder_pid,
            )
            db.add(elder)
            await db.flush()
            call = Call(
                elder_id=elder.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
                profile_override=override_pid,
            )
            db.add(call)
            await db.flush()
            call_id, eid, oid = (
                str(call.id),
                str(elder_pid),
                (str(override_pid) if override_pid else None),
            )
            await db.commit()
            return call_id, eid, oid
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
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
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


def test_agent_config_resolves_from_call_elder_profile(client, async_database_url):
    # A real outbound Call (no override) whose Elder points at published profile P must
    # resolve to P — exercising runtime.get_agent_config's Call/Elder read path.
    call_id, pid, _ = asyncio.run(
        _seed_call_with_elder(async_database_url, elder_voice_id="elder-voice")
    )
    r = client.get(
        "/v1/runtime/agent-config",
        params={"direction": "outbound", "call_id": call_id},
        headers=_wauth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["profile_id"] == pid
    assert body["config"]["voice"]["cartesia_voice_id"] == "elder-voice"


def test_agent_config_call_override_wins_over_elder(client, async_database_url):
    # When the Call carries a profile_override, it wins over the elder's profile.
    call_id, _, override_pid = asyncio.run(
        _seed_call_with_elder(
            async_database_url, elder_voice_id="elder-voice", override_voice_id="override-voice"
        )
    )
    r = client.get(
        "/v1/runtime/agent-config",
        params={"direction": "outbound", "call_id": call_id},
        headers=_wauth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["profile_id"] == override_pid
    assert body["config"]["voice"]["cartesia_voice_id"] == "override-voice"


def test_inbound_resolves_direction_default_not_elder_profile(client, async_database_url):
    # Conscious contract (runtime.get_agent_config docstring): the agent fetches inbound
    # config with call_id=None (dispatch-rule jobs carry no metadata), so an elder's
    # assigned profile is NOT applied on inbound — inbound always resolves to the
    # per-direction default. An elder assigned a DIFFERENT profile exists here, but the
    # call_id-less inbound fetch must still return the inbound default's config.
    asyncio.run(_seed_default(async_database_url, direction="inbound", voice_id="inbound-default"))
    asyncio.run(_seed_call_with_elder(async_database_url, elder_voice_id="elder-assigned"))
    r = client.get("/v1/runtime/agent-config", params={"direction": "inbound"}, headers=_wauth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["config"]["voice"]["cartesia_voice_id"] == "inbound-default"


def test_agent_config_not_rate_limited(client, async_database_url):
    # Runtime route must not be throttled by the operator rate limiter.
    asyncio.run(_seed_default(async_database_url, direction="inbound", voice_id="vin"))
    for _ in range(20):
        r = client.get(
            "/v1/runtime/agent-config", params={"direction": "inbound"}, headers=_wauth()
        )
        assert r.status_code == 200
