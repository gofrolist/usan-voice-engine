"""T038 — contract + integration tests for the RetellAI-compatible
``POST /create-batch-call`` endpoint (feature 003, US4).

Driven over the real HTTP path against the mounted compat sub-app (the
``compat_client`` + ``compat_headers`` fixtures, mirroring ``test_compat_calls.py``).
Asserts: the **unversioned** root path; the Retell-shaped batch object; the one
deliberate ms→seconds exception (``scheduled_timestamp`` is Unix **seconds** while
the request ``trigger_timestamp`` is **ms**); per-task number→Contact lazy upsert
(reusing US1's T022 shim); per-task ``override_agent_id`` liveness; that each task
becomes a ``call_batch_target`` of a ``scheduled`` batch (the poller — gated
per-target — dials later); synthesized-idempotency no-double-batch on retry; and
the RetellAI ``{status,message}`` error envelope.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.schemas.voice_catalog import VOICE_CATALOG

_RETELL_VOICE = "retell-" + VOICE_CATALOG[0].name.split(" - ")[0].split()[0]


def _create_batch(client, headers, **overrides):
    body: dict = {
        "from_number": "+15551230000",
        "tasks": [{"to_number": "+15557654321"}, {"to_number": "+15557650000"}],
    }
    body.update(overrides)
    return client.post("/create-batch-call", json=body, headers=headers)


def _published_agent_id(client, headers) -> str:
    """Two-step create (create-retell-llm → create-agent) yields a PUBLISHED agent
    whose id is a live profile — a valid per-task ``override_agent_id``."""
    llm = client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=headers,
    ).json()
    agent = client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": _RETELL_VOICE,
            "agent_name": "Batch Bot",
        },
        headers=headers,
    ).json()
    return agent["agent_id"]


def _batch_uuid(batch_call_id: str) -> uuid.UUID:
    assert batch_call_id.startswith("batch_call_")
    return uuid.UUID(hex=batch_call_id[len("batch_call_") :])


# --- direct DB inspection (superuser engine bypasses RLS) -------------------------------
async def _batch_row(super_async_url, batch_id: uuid.UUID):
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT name, status, max_concurrency, trigger_at, payload_digest "
                        "FROM call_batches WHERE id = :id"
                    ),
                    {"id": batch_id},
                )
            ).one_or_none()
    finally:
        await engine.dispose()


async def _target_rows(super_async_url, batch_id: uuid.UUID):
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT target_index, contact_id, dynamic_vars, profile_override "
                        "FROM call_batch_targets WHERE batch_id = :id ORDER BY target_index"
                    ),
                    {"id": batch_id},
                )
            ).all()
    finally:
        await engine.dispose()


async def _contact_row(super_async_url, phone):
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT name, timezone, external_id FROM contacts WHERE phone_e164 = :p"),
                    {"p": phone},
                )
            ).one_or_none()
    finally:
        await engine.dispose()


async def _count_batches(super_async_url) -> int:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM call_batches"))).scalar_one()
    finally:
        await engine.dispose()


# --- happy path / contract -------------------------------------------------------------
def test_create_batch_unversioned_path_returns_201_retell_shape(
    compat_client, compat_headers, async_database_url
):
    r = _create_batch(compat_client, compat_headers, name="Spring Campaign")
    assert r.status_code == 201
    body = r.json()
    assert body["batch_call_id"].startswith("batch_call_")
    assert body["name"] == "Spring Campaign"
    assert body["from_number"] == "+15551230000"
    assert body["total_task_count"] == 2
    assert isinstance(body["scheduled_timestamp"], int)

    row = asyncio.run(_batch_row(async_database_url, _batch_uuid(body["batch_call_id"])))
    assert row is not None
    name, status, _maxc, _trig, _digest = row
    assert name == "Spring Campaign"
    assert status == "scheduled"  # awaits the gated poller; never dialed at create time


def test_scheduled_timestamp_is_seconds_while_trigger_is_ms(compat_client, compat_headers):
    trigger_ms = 1_900_000_000_000  # 13-digit epoch MS (≈ year 2030)
    r = _create_batch(compat_client, compat_headers, trigger_timestamp=trigger_ms)
    assert r.status_code == 201
    scheduled = r.json()["scheduled_timestamp"]
    # The one deliberate Retell-faithful exception: response is SECONDS, request is MS.
    assert scheduled == trigger_ms // 1000
    assert len(str(trigger_ms)) == 13
    assert len(str(scheduled)) == 10


def test_immediate_batch_scheduled_timestamp_is_recent_seconds(compat_client, compat_headers):
    r = _create_batch(compat_client, compat_headers)  # no trigger_timestamp = immediate
    assert r.status_code == 201
    scheduled = r.json()["scheduled_timestamp"]
    assert len(str(scheduled)) == 10  # seconds, ≈ now (created_at)


def test_each_task_lazy_upserts_contact(compat_client, compat_headers, async_database_url):
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [
                {"to_number": "+15557654321"},
                {
                    "to_number": "+15550009999",
                    "metadata": {"name": "Bob Smith", "external_id": "crm-42"},
                },
            ],
        },
        headers=compat_headers,
    )
    assert r.status_code == 201
    default = asyncio.run(_contact_row(async_database_url, "+15557654321"))
    assert default is not None
    assert default[0] == "+15557654321"  # default name = the E.164 number
    assert default[1] == "America/New_York"  # COMPAT_DEFAULT_TIMEZONE
    named = asyncio.run(_contact_row(async_database_url, "+15550009999"))
    assert named[0] == "Bob Smith"
    assert named[2] == "crm-42"


def test_targets_created_per_task_with_packed_vars(
    compat_client, compat_headers, async_database_url
):
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [
                {
                    "to_number": "+15557654321",
                    "retell_llm_dynamic_variables": {"first_name": "Ada"},
                    "metadata": {"plan": "gold", "count": 3},
                },
                {"to_number": "+15557650000"},
            ],
        },
        headers=compat_headers,
    )
    assert r.status_code == 201
    batch_id = _batch_uuid(r.json()["batch_call_id"])
    targets = asyncio.run(_target_rows(async_database_url, batch_id))
    assert [t[0] for t in targets] == [0, 1]  # target_index = submitted array order
    assert all(t[1] is not None for t in targets)  # each linked to an upserted contact
    first_vars = targets[0][2]
    assert first_vars["first_name"] == "Ada"
    # metadata is packed under the reserved __meta__ key with full type fidelity.
    assert json.loads(first_vars["__meta__"]) == {"plan": "gold", "count": 3}


def test_reserved_concurrency_maps_to_max_concurrency(
    compat_client, compat_headers, async_database_url
):
    r = _create_batch(compat_client, compat_headers, reserved_concurrency=3)
    assert r.status_code == 201
    row = asyncio.run(_batch_row(async_database_url, _batch_uuid(r.json()["batch_call_id"])))
    assert row[2] == 3  # max_concurrency


def test_call_time_window_is_echoed(compat_client, compat_headers):
    # Oracle shape: windows[].start/end are minutes since midnight (not start_hour/end_hour).
    # The typed CallTimeWindow requires ``windows`` (list[TimeWindow], minItems=1).
    window = {"windows": [{"start": 540, "end": 1020}], "timezone": "America/New_York"}
    r = _create_batch(compat_client, compat_headers, call_time_window=window)
    assert r.status_code == 201
    assert r.json()["call_time_window"]["windows"] == [{"start": 540, "end": 1020}]
    assert r.json()["call_time_window"]["timezone"] == "America/New_York"


# --- per-task override liveness --------------------------------------------------------
def test_per_task_override_published_agent_ok(compat_client, compat_headers, async_database_url):
    agent_id = _published_agent_id(compat_client, compat_headers)
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15557654321", "override_agent_id": agent_id}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 201
    targets = asyncio.run(_target_rows(async_database_url, _batch_uuid(r.json()["batch_call_id"])))
    assert targets[0][3] is not None  # profile_override set on the target


def test_per_task_override_unknown_agent_returns_422(
    compat_client, compat_headers, async_database_url
):
    before = asyncio.run(_count_batches(async_database_url))
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [
                {"to_number": "+15557654321", "override_agent_id": "agent_" + uuid.uuid4().hex}
            ],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
    assert r.json()["status"] == 422
    assert asyncio.run(_count_batches(async_database_url)) == before  # nothing persisted


def test_malformed_override_id_returns_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15557654321", "override_agent_id": "not-an-agent-id"}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422


# --- validation (all-or-nothing) -------------------------------------------------------
def test_invalid_to_number_returns_422_and_persists_nothing(
    compat_client, compat_headers, async_database_url
):
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15557654321"}, {"to_number": "not-a-number"}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
    assert "1" in r.json()["message"]  # names the offending task index
    # All-or-nothing: the VALID sibling's contact must NOT have been upserted.
    assert asyncio.run(_contact_row(async_database_url, "+15557654321")) is None


def test_duplicate_to_number_returns_422(compat_client, compat_headers, async_database_url):
    before = asyncio.run(_count_batches(async_database_url))
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15557654321"}, {"to_number": "+15557654321"}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
    assert asyncio.run(_count_batches(async_database_url)) == before


def test_duplicate_external_id_within_batch_returns_422(
    compat_client, compat_headers, async_database_url
):
    # Two tasks sharing one external_id would collide on uq_contacts_external_id_org at
    # upsert time; pass-1 dedup turns that into a clean 422 (not a 500), nothing persisted.
    before = asyncio.run(_count_batches(async_database_url))
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [
                {"to_number": "+15557654321", "metadata": {"external_id": "dup-1"}},
                {"to_number": "+15557650000", "metadata": {"external_id": "dup-1"}},
            ],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
    assert r.json()["status"] == 422
    assert asyncio.run(_count_batches(async_database_url)) == before


def test_external_id_colliding_existing_contact_returns_422(
    compat_client, compat_headers, async_database_url
):
    # A prior batch creates a contact owning external_id "owned"; a later batch reusing it
    # on a DIFFERENT number hits the unique constraint in pass 2 → clean 422, not a 500.
    r1 = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15551112222", "metadata": {"external_id": "owned"}}],
        },
        headers=compat_headers,
    )
    assert r1.status_code == 201
    before = asyncio.run(_count_batches(async_database_url))
    r2 = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [{"to_number": "+15553334444", "metadata": {"external_id": "owned"}}],
        },
        headers=compat_headers,
    )
    assert r2.status_code == 422
    assert asyncio.run(_count_batches(async_database_url)) == before  # no second batch persisted


def test_invalid_from_number_returns_422(compat_client, compat_headers):
    r = _create_batch(compat_client, compat_headers, from_number="garbage")
    assert r.status_code == 422


def test_empty_tasks_returns_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-batch-call",
        json={"from_number": "+15551230000", "tasks": []},
        headers=compat_headers,
    )
    assert r.status_code == 422


def test_reserved_meta_var_key_rejected(compat_client, compat_headers):
    r = compat_client.post(
        "/create-batch-call",
        json={
            "from_number": "+15551230000",
            "tasks": [
                {"to_number": "+15557654321", "retell_llm_dynamic_variables": {"__metabad": 1}}
            ],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422


# --- idempotent replay -----------------------------------------------------------------
def test_identical_resubmit_replays_same_batch_no_double(
    compat_client, compat_headers, async_database_url
):
    r1 = _create_batch(compat_client, compat_headers, name="ReplayCampaign")
    r2 = _create_batch(compat_client, compat_headers, name="ReplayCampaign")  # identical payload
    assert r1.status_code == 201
    assert r2.json()["batch_call_id"] == r1.json()["batch_call_id"]
    assert asyncio.run(_count_batches(async_database_url)) == 1  # exactly one batch persisted
