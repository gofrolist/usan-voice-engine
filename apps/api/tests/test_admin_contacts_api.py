import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


async def _seed_contact(async_database_url: str, name: str, phone: str) -> str:
    eid = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO contacts (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :n, :p, 'America/New_York')"
                ),
                {"id": eid, "n": name, "p": phone},
            )
    finally:
        await engine.dispose()
    return eid


async def _seed_admin(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "r": role},
            )
    finally:
        await engine.dispose()


def test_contacts_requires_session(client):
    assert client.get("/v1/admin/contacts").status_code == 401


def test_list_and_assign_profile(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada Lovelace", "+15551230001"))
    pid = client.post("/v1/admin/profiles", json={"name": "p-contact"}).json()["id"]

    listed = client.get("/v1/admin/contacts").json()
    me = next(e for e in listed if e["id"] == eid)
    assert me["name"] == "Ada Lovelace"
    assert me["masked_phone"].endswith("0001")
    assert me["masked_phone"].startswith("***")
    assert me["agent_profile_id"] is None

    r = client.put(f"/v1/admin/contacts/{eid}/profile", json={"agent_profile_id": pid})
    assert r.status_code == 200
    assert r.json()["agent_profile_id"] == pid
    assert r.json()["agent_profile_name"] == "p-contact"

    r2 = client.put(f"/v1/admin/contacts/{eid}/profile", json={"agent_profile_id": None})
    assert r2.status_code == 200
    assert r2.json()["agent_profile_id"] is None


def test_assign_unknown_contact_404(client, admin_session):
    r = client.put(f"/v1/admin/contacts/{uuid.uuid4()}/profile", json={"agent_profile_id": None})
    assert r.status_code == 404


def test_assign_unknown_profile_400(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Grace Hopper", "+15551230002"))
    r = client.put(
        f"/v1/admin/contacts/{eid}/profile", json={"agent_profile_id": str(uuid.uuid4())}
    )
    assert r.status_code == 400


def test_contacts_pagination(client, admin_session, async_database_url):
    asyncio.run(_seed_contact(async_database_url, "AAA Pager", "+15551240001"))
    asyncio.run(_seed_contact(async_database_url, "BBB Pager", "+15551240002"))
    asyncio.run(_seed_contact(async_database_url, "CCC Pager", "+15551240003"))
    page1 = client.get("/v1/admin/contacts?limit=2&offset=0").json()
    assert len(page1) == 2
    page2 = client.get("/v1/admin/contacts?limit=2&offset=2").json()
    assert len(page2) >= 1
    ids1 = {e["id"] for e in page1}
    assert all(e["id"] not in ids1 for e in page2)  # pages don't overlap
    # Over-cap limit is rejected by Query(le=500).
    assert client.get("/v1/admin/contacts?limit=100000").status_code == 422


def test_assign_profile_audit_detail_has_no_phi(client, admin_session, async_database_url):
    # HIPAA invariant: the contact.assign_profile audit detail carries ONLY the profile
    # UUID — never the contact's name or phone. Lock it so a future writer can't leak PHI.
    eid = asyncio.run(_seed_contact(async_database_url, "Ada Lovelace", "+15551239999"))
    pid = client.post("/v1/admin/profiles", json={"name": "p-phi"}).json()["id"]
    client.put(f"/v1/admin/contacts/{eid}/profile", json={"agent_profile_id": pid})
    rows = client.get("/v1/admin/audit?action=contact.assign_profile").json()
    entry = next(e for e in rows if e["entity_id"] == eid)
    assert entry["detail"] == {"agent_profile_id": pid}
    # Strip the (already exactly-asserted) profile UUID before the substring checks:
    # "ada"/"9999" are valid hex, so a random UUID can contain them (observed flake).
    blob = (str(entry["detail"]).replace(pid, "") + str(entry["entity_type"])).lower()
    assert "ada" not in blob
    assert "lovelace" not in blob
    assert "9999" not in blob


def test_summary_includes_timezone(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Shown", "+15551250001"))
    listed = client.get("/v1/admin/contacts").json()
    me = next(e for e in listed if e["id"] == eid)
    assert me["timezone"] == "America/New_York"


def test_set_timezone_happy_path_and_audit(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Set", "+15551250002"))
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "America/Chicago"})
    assert r.status_code == 200
    assert r.json()["timezone"] == "America/Chicago"
    listed = client.get("/v1/admin/contacts").json()
    assert next(e for e in listed if e["id"] == eid)["timezone"] == "America/Chicago"
    rows = client.get("/v1/admin/audit?action=contact.set_timezone").json()
    entry = next(e for e in rows if e["entity_id"] == eid)
    assert entry["detail"] == {"old": "America/New_York", "new": "America/Chicago"}


def test_set_timezone_invalid_iana_422_leaves_value_unchanged(
    client, admin_session, async_database_url
):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Bad", "+15551250003"))
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "Mars/Phobos"})
    assert r.status_code == 422
    listed = client.get("/v1/admin/contacts").json()
    assert next(e for e in listed if e["id"] == eid)["timezone"] == "America/New_York"


def test_set_timezone_unknown_contact_404(client, admin_session):
    r = client.put(
        f"/v1/admin/contacts/{uuid.uuid4()}/timezone", json={"timezone": "America/Chicago"}
    )
    assert r.status_code == 404


def test_viewer_cannot_set_timezone(client, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Viewer", "+15551250004"))
    asyncio.run(_seed_admin(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "America/Chicago"})
    assert r.status_code == 403
