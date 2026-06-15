"""T032 (US2): operator admin plane for family contacts & tasks.

Contact CRUD, task list (needs-review first) + operator transitions (approve a held task
→ injectable; close), session/role gating, and the no-PHI audit invariant.
"""

import asyncio
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.settings import get_settings


async def _seed_contact(async_database_url: str, name: str, phone: str) -> str:
    eid = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO contacts (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :n, :p, 'UTC')"
                ),
                {"id": eid, "n": name, "p": phone},
            )
    finally:
        await engine.dispose()
    return eid


async def _seed_task(
    async_database_url: str,
    contact_id: str,
    message: str,
    *,
    needs_safety_review: bool = False,
    status: str = "open",
) -> int:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        "INSERT INTO family_tasks "
                        "(contact_id, message, status, needs_safety_review) "
                        "VALUES (CAST(:e AS uuid), :m, :s, :r) RETURNING id"
                    ),
                    {"e": contact_id, "m": message, "s": status, "r": needs_safety_review},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


def _viewer_cookies(async_database_url: str) -> dict[str, str]:
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole

    email = "viewer@example.com"

    async def _seed():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO admin_users (email, role, added_by) "
                        "VALUES (:e, CAST('viewer' AS admin_role), 'test') "
                        "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                    ),
                    {"e": email},
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    return {SESSION_COOKIE_NAME: issue_session(email, AdminRole.VIEWER, get_settings())}


def test_family_endpoints_require_session(client):
    assert client.get(f"/v1/admin/family-contacts?contact_id={uuid.uuid4()}").status_code == 401
    assert client.get("/v1/admin/family-tasks").status_code == 401


def test_contact_crud_roundtrip(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230101"))
    r = client.post(
        "/v1/admin/family-contacts",
        json={
            "contact_id": eid,
            "name": "Maria",
            "phone_e164": "+15551234567",
            "relationship": "daughter",
            "alert_prefs": {"crisis": True, "missed_call": False},
        },
    )
    assert r.status_code == 201, r.text
    contact = r.json()
    assert contact["name"] == "Maria"
    assert contact["phone_e164"] == "+15551234567"
    assert contact["alert_prefs"] == {"crisis": True, "missed_call": False}
    cid = contact["id"]

    listed = client.get(f"/v1/admin/family-contacts?contact_id={eid}").json()
    assert [c["id"] for c in listed] == [cid]

    patched = client.patch(
        f"/v1/admin/family-contacts/{cid}",
        json={"relationship": "granddaughter", "alert_prefs": {"crisis": True}},
    )
    assert patched.status_code == 200
    assert patched.json()["relationship"] == "granddaughter"
    assert patched.json()["alert_prefs"] == {"crisis": True}

    assert client.delete(f"/v1/admin/family-contacts/{cid}").status_code == 204
    assert client.get(f"/v1/admin/family-contacts?contact_id={eid}").json() == []


def test_create_contact_unknown_contact_404(client, admin_session):
    r = client.post(
        "/v1/admin/family-contacts",
        json={"contact_id": str(uuid.uuid4()), "name": "X", "phone_e164": "+15551230000"},
    )
    assert r.status_code == 404


def test_create_contact_rejects_bad_phone_422(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230102"))
    r = client.post(
        "/v1/admin/family-contacts",
        json={"contact_id": eid, "name": "X", "phone_e164": "555-not-e164"},
    )
    assert r.status_code == 422


def test_contact_mutation_forbidden_for_viewer(client, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230103"))
    client.cookies.update(_viewer_cookies(async_database_url))
    r = client.post(
        "/v1/admin/family-contacts",
        json={"contact_id": eid, "name": "Maria", "phone_e164": "+15551234567"},
    )
    assert r.status_code == 403  # viewer may read, not mutate


def test_family_tasks_list_needs_review_first(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230104"))
    asyncio.run(_seed_task(async_database_url, eid, "normal task"))
    asyncio.run(_seed_task(async_database_url, eid, "held task", needs_safety_review=True))
    rows = client.get(f"/v1/admin/family-tasks?contact_id={eid}").json()
    assert len(rows) == 2
    # needs-review surfaces first regardless of insertion order.
    assert rows[0]["needs_safety_review"] is True
    assert rows[0]["message"] == "held task"


def test_patch_task_close(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230105"))
    tid = asyncio.run(_seed_task(async_database_url, eid, "do a thing"))
    r = client.patch(f"/v1/admin/family-tasks/{tid}", json={"status": "closed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"
    assert r.json()["status_updated_by"] == "admin@example.com"


def test_patch_task_approve_clears_review(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230106"))
    tid = asyncio.run(_seed_task(async_database_url, eid, "held", needs_safety_review=True))
    r = client.patch(f"/v1/admin/family-tasks/{tid}", json={"status": "open"})
    assert r.status_code == 200, r.text
    assert r.json()["needs_safety_review"] is False
    assert r.json()["status"] == "open"


def test_patch_task_unknown_404(client, admin_session):
    r = client.patch("/v1/admin/family-tasks/999999", json={"status": "closed"})
    assert r.status_code == 404


def test_contact_audit_has_no_phi(client, admin_session, async_database_url):
    # HIPAA invariant: the family_contact.create audit detail carries ONLY the contact UUID —
    # never the contact's name or phone number.
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230107"))
    cid = client.post(
        "/v1/admin/family-contacts",
        json={"contact_id": eid, "name": "Maria", "phone_e164": "+15559998888"},
    ).json()["id"]
    rows = client.get("/v1/admin/audit?action=family_contact.create").json()
    entry = next(e for e in rows if e["entity_id"] == cid)
    assert entry["detail"] == {"contact_id": eid}
    blob = (str(entry["detail"]).replace(eid, "") + str(entry["entity_type"])).lower()
    assert "maria" not in blob
    assert "9998888" not in blob


# --- family reports list + resend (US8 / T079; coverage T081, audit-no-PHI T082) ----------


async def _seed_family_report(
    async_database_url: str,
    contact_id: str,
    *,
    period_month: date = date(2026, 5, 1),
    status: str = "sent",
) -> int:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        "INSERT INTO family_reports "
                        "(contact_id, period_month, status, calls_completed, metrics, narrative, "
                        "model_version) VALUES (CAST(:e AS uuid), :pm, :s, :cc, "
                        "CAST(:m AS jsonb), :n, :mv) RETURNING id"
                    ),
                    {
                        "e": contact_id,
                        "pm": period_month,
                        "s": status,
                        "cc": 20,
                        "m": '{"avg_mood": 4.1}',
                        "n": "internal trend narrative (PHI, stays in Postgres)",
                        "mv": "deterministic",
                    },
                )
            ).scalar_one()
    finally:
        await engine.dispose()


def test_family_reports_require_session(client):
    assert client.get("/v1/admin/family-reports").status_code == 401


def test_list_family_reports_returns_rich_trend_row(client, admin_session, async_database_url):
    # Operator (BAA) plane: the full report row — metrics + narrative + model_version + contact
    # name — is visible here; the PHI-minimization applies only to the outbound SMS.
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230201"))
    rid = asyncio.run(_seed_family_report(async_database_url, eid))
    rows = client.get(f"/v1/admin/family-reports?contact_id={eid}").json()
    row = next(r for r in rows if r["id"] == rid)
    assert row["contact_name"] == "Ada"
    assert row["status"] == "sent"
    assert row["metrics"] == {"avg_mood": 4.1}
    assert row["model_version"] == "deterministic"
    assert row["calls_completed"] == 20


def test_resend_family_report_reenqueues_phi_free_sms_and_audits(
    client, admin_session, async_database_url
):
    from usan_api import notifications

    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230202"))
    # A family contact opted in for reports (default alert_prefs => opted in for "report").
    client.post(
        "/v1/admin/family-contacts",
        json={"contact_id": eid, "name": "Maria", "phone_e164": "+15559990001"},
    )
    rid = asyncio.run(_seed_family_report(async_database_url, eid))

    r = client.post(f"/v1/admin/family-reports/{rid}/resend")
    assert r.status_code == 200, r.text

    # Exactly one PHI-free family_report SMS was enqueued, body == the fixed template.
    async def _bodies() -> list[str]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT body FROM sms_messages WHERE contact_id = CAST(:e AS uuid) "
                            "AND kind = 'family_report'"
                        ),
                        {"e": eid},
                    )
                ).all()
                return [row[0] for row in rows]
        finally:
            await engine.dispose()

    bodies = asyncio.run(_bodies())
    assert bodies == [notifications.build_family_report_body()]
    low = bodies[0].lower()
    for term in (
        "mood",
        "pain",
        "medication",
        "lonely",
        "loneliness",
        "narrative",
        "adherence",
        "survey",
        "4.1",
    ):
        assert term not in low, f"family report SMS leaks clinical term: {term}"

    # Audit detail carries ONLY a recipient COUNT — never a phone or contact name.
    audit = client.get("/v1/admin/audit?action=family_report.resend").json()
    entry = next(e for e in audit if e["entity_id"] == str(rid))
    assert entry["detail"] == {"recipients": 1}
    blob = str(entry["detail"]).lower()
    assert "maria" not in blob
    assert "9990001" not in blob


def test_resend_unknown_report_404(client, admin_session):
    assert client.post("/v1/admin/family-reports/999999/resend").status_code == 404


def test_resend_no_contact_report_409(client, admin_session, async_database_url):
    # A report whose contact has no opted-in family contact has nobody to resend to.
    eid = asyncio.run(_seed_contact(async_database_url, "Bo", "+15551230203"))
    rid = asyncio.run(_seed_family_report(async_database_url, eid, status="no_contact"))
    assert client.post(f"/v1/admin/family-reports/{rid}/resend").status_code == 409


def test_resend_forbidden_for_viewer(client, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Ada", "+15551230204"))
    rid = asyncio.run(_seed_family_report(async_database_url, eid))
    client.cookies.update(_viewer_cookies(async_database_url))
    assert client.post(f"/v1/admin/family-reports/{rid}/resend").status_code == 403
