import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _seed_elder(async_database_url: str, name: str, phone: str) -> str:
    eid = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :n, :p, 'America/New_York')"
                ),
                {"id": eid, "n": name, "p": phone},
            )
    finally:
        await engine.dispose()
    return eid


def test_elders_requires_session(client):
    assert client.get("/v1/admin/elders").status_code == 401


def test_list_and_assign_profile(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_elder(async_database_url, "Ada Lovelace", "+15551230001"))
    pid = client.post("/v1/admin/profiles", json={"name": "p-elder"}).json()["id"]

    listed = client.get("/v1/admin/elders").json()
    me = next(e for e in listed if e["id"] == eid)
    assert me["name"] == "Ada Lovelace"
    assert me["masked_phone"].endswith("0001")
    assert me["masked_phone"].startswith("***")
    assert me["agent_profile_id"] is None

    r = client.put(f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": pid})
    assert r.status_code == 200
    assert r.json()["agent_profile_id"] == pid
    assert r.json()["agent_profile_name"] == "p-elder"

    r2 = client.put(f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": None})
    assert r2.status_code == 200
    assert r2.json()["agent_profile_id"] is None


def test_assign_unknown_elder_404(client, admin_session):
    r = client.put(f"/v1/admin/elders/{uuid.uuid4()}/profile", json={"agent_profile_id": None})
    assert r.status_code == 404


def test_assign_unknown_profile_400(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_elder(async_database_url, "Grace Hopper", "+15551230002"))
    r = client.put(f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": str(uuid.uuid4())})
    assert r.status_code == 400
