"""Operator CRUD API for /v1/schedules (spec §4.1).

Covers the full router contract: next_run_at computation at create (aware UTC),
404/409/422 mappings (unknown contact, duplicate schedule, zoneinfo fail-closed,
quiet-hours-empty window, unpublished profile_override), the last_result filter
("who missed today's call"), PATCH merge + revalidate + recompute — including
the contact-timezone-went-bad 422-not-500 pin — DELETE, ids-only audit logging
(never contact name / dynamic_vars, spec §8), and the operator-token gate.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import call_schedules as schedules_repo

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _seed_contact(client, *, timezone: str = "America/New_York", name: str = "Rose Contact") -> str:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    r = client.post(
        "/v1/contacts",
        json={"name": name, "phone_e164": phone, "timezone": timezone},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _schedule_body(contact_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contact_id": contact_id,
        "window_start_local": "09:00",
        "window_end_local": "17:00",
    }
    body.update(overrides)
    return body


def _parse_aware_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, "next_run_at must be timezone-aware"
    assert dt.utcoffset() == timedelta(0), "next_run_at must be UTC"
    return dt


async def _run_db(async_database_url: str, fn) -> Any:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            result = await fn(db)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def test_create_schedule_201_computes_next_run_at(client):
    contact_id = _seed_contact(client)
    before = datetime.now(UTC)
    r = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert r.status_code == 201
    body = r.json()
    assert body["contact_id"] == contact_id
    assert body["enabled"] is True
    assert body["slot"] == "morning"  # US5 default
    assert body["days_of_week"] == ALL_DAYS  # echoed as the string list
    next_run = _parse_aware_utc(body["next_run_at"])
    # Earliest occurrence >= now: never in the past (allow clock-read slack).
    assert next_run >= before - timedelta(seconds=5)
    assert body["last_result"] is None
    assert body["last_materialized_date"] is None


def test_create_schedule_404_unknown_contact(client):
    r = client.post("/v1/schedules", json=_schedule_body(str(uuid.uuid4())), headers=_OP)
    assert r.status_code == 404


def test_create_schedule_409_second_schedule_same_contact(client):
    # No slot given -> both default to 'morning' -> the per-(contact, slot) 409.
    contact_id = _seed_contact(client)
    first = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert first.status_code == 201
    r = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert r.status_code == 409


def test_create_two_slots_then_per_slot_409_and_filter(client):
    # US5: an contact may have a morning AND an evening schedule; the 409 is per slot.
    contact_id = _seed_contact(client)
    morning = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert morning.status_code == 201
    assert morning.json()["slot"] == "morning"

    evening_body = _schedule_body(
        contact_id, slot="evening", window_start_local="18:00", window_end_local="20:00"
    )
    evening = client.post("/v1/schedules", json=evening_body, headers=_OP)
    assert evening.status_code == 201
    assert evening.json()["slot"] == "evening"

    # A duplicate evening collides on (contact_id, slot) -> 409 naming the slot.
    dup = client.post("/v1/schedules", json=evening_body, headers=_OP)
    assert dup.status_code == 409
    assert "evening" in dup.json()["detail"]

    # ?slot= narrows the list; without it both slots come back.
    only_evening = client.get(
        "/v1/schedules", params={"contact_id": contact_id, "slot": "evening"}, headers=_OP
    )
    assert only_evening.status_code == 200
    assert [r["slot"] for r in only_evening.json()] == ["evening"]
    both = client.get("/v1/schedules", params={"contact_id": contact_id}, headers=_OP)
    assert {r["slot"] for r in both.json()} == {"morning", "evening"}


def test_patch_slot_and_invalid_slot_filter_are_422(bare_client):
    # slot is immutable identity: a PATCH carrying it 422s (extra="forbid"), never a
    # silent no-op that would let the caller believe a move succeeded.
    r = bare_client.patch(f"/v1/schedules/{uuid.uuid4()}", json={"slot": "evening"}, headers=_OP)
    assert r.status_code == 422
    # The ?slot= filter is the closed morning|evening enum: an unknown value 422s at
    # the boundary instead of returning a misleading empty 200.
    bad = bare_client.get("/v1/schedules", params={"slot": "afternoon"}, headers=_OP)
    assert bad.status_code == 422


def test_create_schedule_422_invalid_contact_timezone(client, async_database_url):
    # The contact API now rejects bad zones at the boundary, but a row can still hold
    # an unresolvable zone (legacy data / direct DB write); schedule creation must fail
    # closed (zoneinfo ValueError -> 422, spec §6.3). Corrupt the zone at the DB level
    # to exercise the create path's fail-closed branch.
    contact_id = _seed_contact(client)

    async def _corrupt_tz(db):
        await db.execute(
            text("UPDATE contacts SET timezone = 'Mars/Olympus' WHERE id = :id"),
            {"id": contact_id},
        )

    asyncio.run(_run_db(async_database_url, _corrupt_tz))

    r = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert r.status_code == 422


def test_create_schedule_defensive_none_maps_to_422(client, monkeypatch):
    # Defensive branch: next_run_at returns None only for policy-induced empty
    # intersections (§3.3.3 rule 2) and this router never passes policy bounds —
    # but if the branch is ever reached it must fail closed through the same
    # handled 422 path as the other ValueErrors, never escape as a 500.
    from usan_api.services import schedules as schedules_svc

    contact_id = _seed_contact(client)
    monkeypatch.setattr(schedules_svc, "next_run_at", lambda *a, **k: None)
    r = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP)
    assert r.status_code == 422


def test_create_schedule_422_window_outside_quiet_hours(client):
    contact_id = _seed_contact(client)
    r = client.post(
        "/v1/schedules",
        json=_schedule_body(contact_id, window_start_local="06:00", window_end_local="08:00"),
        headers=_OP,
    )
    assert r.status_code == 422


def test_create_schedule_422_unpublished_profile_override(client, async_database_url):
    contact_id = _seed_contact(client)

    async def _seed_draft(db):
        profile = await agent_profiles_repo.create_profile(
            db, name="Draft profile", description=None, actor_email="op@example.com"
        )
        return str(profile.id)

    profile_id = asyncio.run(_run_db(async_database_url, _seed_draft))
    r = client.post(
        "/v1/schedules",
        json=_schedule_body(contact_id, profile_override=profile_id),
        headers=_OP,
    )
    assert r.status_code == 422  # not live: no published version (C2 helper)


def test_list_schedules_filters_last_result(client, async_database_url):
    miss_contact = _seed_contact(client)
    ok_contact = _seed_contact(client)
    miss = client.post("/v1/schedules", json=_schedule_body(miss_contact), headers=_OP).json()
    client.post("/v1/schedules", json=_schedule_body(ok_contact), headers=_OP)

    async def _mark_skipped(db):
        schedule = await schedules_repo.get_schedule(db, uuid.UUID(miss["id"]))
        assert schedule is not None
        await schedules_repo.record_result(
            db, schedule, result="skipped_window", now=datetime.now(UTC)
        )

    asyncio.run(_run_db(async_database_url, _mark_skipped))

    r = client.get("/v1/schedules", params={"last_result": "skipped_window"}, headers=_OP)
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [miss["id"]]  # only the miss (spec §4.1)

    r_all = client.get("/v1/schedules", headers=_OP)
    assert r_all.status_code == 200
    assert len(r_all.json()) == 2


def test_get_schedule_200_and_404(client):
    contact_id = _seed_contact(client)
    created = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP).json()
    r = client.get(f"/v1/schedules/{created['id']}", headers=_OP)
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]
    assert client.get(f"/v1/schedules/{uuid.uuid4()}", headers=_OP).status_code == 404


def test_patch_recomputes_next_run_at_and_revalidates(client):
    contact_id = _seed_contact(client)
    created = client.post(
        "/v1/schedules",
        json=_schedule_body(contact_id, window_start_local="09:00", window_end_local="10:00"),
        headers=_OP,
    ).json()

    # Disjoint window: the recomputed next_run_at can never equal the original.
    r = client.patch(
        f"/v1/schedules/{created['id']}",
        json={"window_start_local": "10:00", "window_end_local": "11:00"},
        headers=_OP,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_start_local"].startswith("10:00")
    assert _parse_aware_utc(body["next_run_at"]) != _parse_aware_utc(created["next_run_at"])

    # A window that never intersects quiet hours [09:00, 21:00) -> 422.
    r_bad = client.patch(
        f"/v1/schedules/{created['id']}",
        json={"window_start_local": "06:00", "window_end_local": "08:00"},
        headers=_OP,
    )
    assert r_bad.status_code == 422

    # enabled=false pauses the schedule.
    r_pause = client.patch(f"/v1/schedules/{created['id']}", json={"enabled": False}, headers=_OP)
    assert r_pause.status_code == 200
    assert r_pause.json()["enabled"] is False


def test_patch_window_422_when_contact_timezone_went_bad(client, async_database_url):
    # contacts.timezone is only length-validated at the contact API boundary, so it can
    # go bad after schedule creation; the recompute's ValueError maps to 422, not 500.
    contact_id = _seed_contact(client)
    created = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP).json()

    async def _corrupt_tz(db):
        await db.execute(
            text("UPDATE contacts SET timezone = 'Mars/Olympus' WHERE id = :id"),
            {"id": contact_id},
        )

    asyncio.run(_run_db(async_database_url, _corrupt_tz))

    r = client.patch(
        f"/v1/schedules/{created['id']}",
        json={"window_start_local": "10:00", "window_end_local": "12:00"},
        headers=_OP,
    )
    assert r.status_code == 422


def test_delete_schedule_204_then_404(client):
    contact_id = _seed_contact(client)
    created = client.post("/v1/schedules", json=_schedule_body(contact_id), headers=_OP).json()
    assert client.delete(f"/v1/schedules/{created['id']}", headers=_OP).status_code == 204
    assert client.get(f"/v1/schedules/{created['id']}", headers=_OP).status_code == 404
    assert client.delete(f"/v1/schedules/{created['id']}", headers=_OP).status_code == 404


def test_mutations_write_audit_log_lines(client):
    contact_name = "Rose Auditcheck"
    contact_id = _seed_contact(client, name=contact_name)
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        created = client.post(
            "/v1/schedules",
            json=_schedule_body(contact_id, dynamic_vars={"first_name": contact_name}),
            headers=_OP,
        ).json()
        client.patch(f"/v1/schedules/{created['id']}", json={"enabled": False}, headers=_OP)
        client.delete(f"/v1/schedules/{created['id']}", headers=_OP)
    finally:
        logger.remove(handler_id)

    audit = [r for r in records if r["extra"].get("schedule_id") == created["id"]]
    actions = {r["extra"].get("action") for r in audit}
    assert {"schedule_created", "schedule_updated", "schedule_deleted"} <= actions
    for record in audit:
        assert record["extra"].get("client"), "audit line must bind the client IP"
        assert record["extra"].get("schedule_id") == created["id"]
        assert record["extra"].get("action")

    # PHI rule (spec §8): ids only — no record binds contact name or dynamic_vars.
    for record in records:
        assert "name" not in record["extra"]
        assert "dynamic_vars" not in record["extra"]
        assert contact_name not in record["message"]
        assert all(contact_name not in str(v) for v in record["extra"].values())


def test_schedules_require_operator_token(bare_client):
    sid = uuid.uuid4()
    assert bare_client.post("/v1/schedules", json={}).status_code == 401
    assert bare_client.get("/v1/schedules").status_code == 401
    assert bare_client.get(f"/v1/schedules/{sid}").status_code == 401
    assert bare_client.patch(f"/v1/schedules/{sid}", json={}).status_code == 401
    assert bare_client.delete(f"/v1/schedules/{sid}").status_code == 401
