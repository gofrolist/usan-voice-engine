import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _seed_callback(
    async_database_url: str,
    *,
    requested_time_text: str,
    status: str = "open",
    notes: str | None = None,
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
                {"id": elder_id, "p": f"+1555{str(uuid.UUID(elder_id).int)[:7].zfill(7)}"},
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
                    "(call_id, elder_id, requested_time_text, status, notes) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), :t, :s, :n)"
                ),
                {
                    "cid": call_id,
                    "eid": elder_id,
                    "t": requested_time_text,
                    "s": status,
                    "n": notes,
                },
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
        "status_updated_at",
        "status_updated_by",
        "created_at",
    }
    assert one["status"] == "open"
    # C2 workflow stamps: NULL until the first transition (no backfill needed).
    assert one["status_updated_at"] is None
    assert one["status_updated_by"] is None


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


def test_list_callback_requests_filters_by_elder(client, admin_session, async_database_url):
    _call_a, elder_a = asyncio.run(
        _seed_callback(async_database_url, requested_time_text="elder-a-cb")
    )
    asyncio.run(_seed_callback(async_database_url, requested_time_text="elder-b-cb"))
    rows = client.get(f"/v1/admin/callback-requests?elder_id={elder_a}").json()
    texts = {r["requested_time_text"] for r in rows}
    assert texts == {"elder-a-cb"}
    assert all(r["elder_id"] == elder_a for r in rows)


def test_list_callback_requests_over_cap_limit_422(client, admin_session):
    assert client.get("/v1/admin/callback-requests?limit=100000").status_code == 422


def test_callback_requests_read_is_audited_phi_free(client, admin_session, async_database_url):
    # Callback notes are PHI; the admin read must be audited, but the audit entry
    # itself must carry NO PHI (no notes text). Mirrors the follow-up-flags audit test.
    asyncio.run(
        _seed_callback(
            async_database_url,
            requested_time_text="tomorrow at 3",
            notes="secret callback note",
        )
    )
    client.get("/v1/admin/callback-requests")
    rows = client.get("/v1/admin/audit?action=callback_requests.list").json()
    assert rows, "callback-requests read must write an audit entry"
    entry = rows[0]
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "secret" not in blob
    assert "callback note" not in blob
