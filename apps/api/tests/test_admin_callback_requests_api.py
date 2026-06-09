import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _seed_callback(
    async_database_url: str, *, requested_time_text: str, status: str = "open"
) -> tuple[str, str]:
    elder_id = str(uuid.uuid4())
    call_id = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), 'Ada', :p, 'UTC')"
                ),
                {"id": elder_id, "p": f"+1555{str(uuid.UUID(elder_id).int)[:7]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'outbound', 'completed')"
                ),
                {"cid": call_id, "eid": elder_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO callback_requests "
                    "(call_id, elder_id, requested_time_text, status) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), :t, :s)"
                ),
                {"cid": call_id, "eid": elder_id, "t": requested_time_text, "s": status},
            )
    finally:
        await engine.dispose()
    return call_id, elder_id


def test_callback_requests_requires_session(client):
    assert client.get("/v1/admin/callback-requests").status_code == 401


def test_list_callback_requests(client, admin_session, async_database_url):
    asyncio.run(_seed_callback(async_database_url, requested_time_text="tomorrow at 3"))
    rows = client.get("/v1/admin/callback-requests").json()
    assert any(r["requested_time_text"] == "tomorrow at 3" for r in rows)
    one = next(r for r in rows if r["requested_time_text"] == "tomorrow at 3")
    assert set(one.keys()) == {
        "id",
        "call_id",
        "elder_id",
        "requested_time_text",
        "requested_at",
        "notes",
        "status",
        "created_at",
    }
    assert one["status"] == "open"


def test_list_callback_requests_filters_by_status(client, admin_session, async_database_url):
    asyncio.run(_seed_callback(async_database_url, requested_time_text="open-one", status="open"))
    asyncio.run(
        _seed_callback(async_database_url, requested_time_text="done-one", status="resolved")
    )
    open_rows = client.get("/v1/admin/callback-requests?status=open").json()
    texts = {r["requested_time_text"] for r in open_rows}
    assert "open-one" in texts
    assert "done-one" not in texts
    assert all(r["status"] == "open" for r in open_rows)


def test_list_callback_requests_over_cap_limit_422(client, admin_session):
    assert client.get("/v1/admin/callback-requests?limit=100000").status_code == 422
