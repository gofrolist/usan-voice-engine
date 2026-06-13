"""US4 (FR-022/FR-023, SC-008) — the "Contacts" rename is shim-first.

The user-facing relabel and the new ``contact_name`` builtin must NOT touch any
external/back-compat surface: the legacy recipient routes (`/v1/elders` CRUD and
`/v1/admin/elders`) keep their paths and field names, and the outbound webhook
payloads keep the signed ``elder_id`` key external consumers depend on. This is a
regression guard so a future over-eager physical rename trips a red test.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import webhook_events

# Management-plane routes require the operator bearer token (matches conftest).
_OP = {"Authorization": "Bearer " + "o" * 32}


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


# ---------------------------------------------------------------------------
# Legacy `/v1/elders` CRUD route + field names are unchanged (no rename).
# ---------------------------------------------------------------------------


def test_elders_crud_route_and_fields_unchanged(client):
    created = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": "+15550009001",
            "timezone": "America/New_York",
            "metadata": {"floor": 3},
        },
        headers=_OP,
    )
    assert created.status_code == 201
    body = created.json()
    # The recipient resource is still keyed/shaped as before the rename.
    assert body["name"] == "Ada"
    assert body["metadata"] == {"floor": 3}
    elder_id = body["id"]
    assert uuid.UUID(elder_id)

    updated = client.put(f"/v1/elders/{elder_id}", json={"name": "Renamed"}, headers=_OP)
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"


def test_no_contacts_route_alias_was_introduced(client):
    # R6: relabel-only, NO `/v1/contacts` alias. A missing route returns 404/405,
    # never 200/201 — assert the alias was NOT silently added alongside the rename.
    r = client.post(
        "/v1/contacts",
        json={"name": "X", "phone_e164": "+15550009099", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code != 201


# ---------------------------------------------------------------------------
# `/v1/admin/elders` list route + field names are unchanged.
# ---------------------------------------------------------------------------


def test_admin_elders_route_and_fields_unchanged(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_elder(async_database_url, "Grace Hopper", "+15550009002"))
    listed = client.get("/v1/admin/elders").json()
    me = next(e for e in listed if e["id"] == eid)
    # The admin recipient row still exposes name / masked_phone / agent_profile_id.
    assert me["name"] == "Grace Hopper"
    assert "masked_phone" in me
    assert "agent_profile_id" in me


# ---------------------------------------------------------------------------
# Outbound webhook payloads keep the signed `elder_id` key (frozen external surface).
# ---------------------------------------------------------------------------


def test_webhook_payload_schemas_still_carry_elder_id():
    # The payload builders are the only place envelopes are constructed; the
    # call.* and callback.created data models must keep declaring elder_id so the
    # signed external payload never drops the key (R6 frozen surface / SC-008).
    assert "elder_id" in webhook_events._CallStartedData.model_fields
    assert "elder_id" in webhook_events._CallCompletedData.model_fields
    assert "elder_id" in webhook_events._CallbackCreatedData.model_fields
