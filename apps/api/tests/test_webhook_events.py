"""webhook_events builders: exact field allowlists, pinned PHI exclusions, origin walk.

Payloads are constructed ONLY by ``usan_api.webhook_events`` (spec §6.1 — allowlist
by construction); these tests pin each event's exact ``data`` key set (§6.2–§6.7),
assert the excluded-everywhere PHI fields stay out via sentinel strings, and prove
``origin`` is derived from the CHAIN ROOT's idempotency_key (§10.9).
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import webhook_events
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import CallBatch, CallBatchTarget
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import callback_requests as callbacks_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import follow_up_flags as flags_repo
from usan_api.schemas.batch import BatchTargetIn

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

# Spec §6.1 envelope: exactly these three keys in the STORED payload — delivery_id
# is injected at send time only.
ENVELOPE_KEYS = {"event", "occurred_at", "data"}


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE call_batch_targets, call_batches, call_schedules, calls, contacts CASCADE"
            )
        )
        await db.commit()


async def _seed_contact(factory, *, name: str = "Events Contact") -> tuple[uuid.UUID, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name=name, phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
        return contact.id, phone


def _batch_kwargs(**overrides):
    base = {
        "name": "events batch",
        "idempotency_key": None,
        "payload_digest": "d" * 64,
        "trigger_at": None,
        "window_start_local": None,
        "window_end_local": None,
        "days_of_week": None,
        "max_concurrency": None,
        "profile_override": None,
    }
    return {**base, **overrides}


async def test_payload_field_allowlists_exact(session_factory):
    contact_id, _phone = await _seed_contact(session_factory)
    async with session_factory() as db:
        inbound = await calls_repo.create_inbound_call(
            db, contact_id=contact_id, livekit_room=f"usan-inbound-{uuid.uuid4()}"
        )
        completed = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
        )
        flag = await flags_repo.create_follow_up_flag(
            db,
            call_id=completed.id,
            contact_id=contact_id,
            severity="routine",
            category="other",
            reason=None,
        )
        callback = await callbacks_repo.create_callback_request(
            db,
            call_id=completed.id,
            contact_id=contact_id,
            requested_time_text="tomorrow morning",
            requested_at=None,
            notes=None,
        )
        batch = await batches_repo.create_batch_with_targets(
            db, targets=[BatchTargetIn(contact_id=contact_id)], **_batch_kwargs()
        )

        table = [
            (
                "call.started",
                await webhook_events.call_started_payload(db, inbound),
                {
                    "call_id",
                    "contact_id",
                    "direction",
                    "attempt",
                    "parent_call_id",
                    "origin",
                    "answered_at",
                },
            ),
            (
                "call.completed",
                await webhook_events.call_completed_payload(db, completed),
                {
                    "call_id",
                    "contact_id",
                    "direction",
                    "status",
                    "attempt",
                    "parent_call_id",
                    "origin",
                    "created_at",
                    "answered_at",
                    "ended_at",
                    "duration_seconds",
                },
            ),
            (
                "flag.created",
                webhook_events.flag_created_payload(flag),
                {"flag_id", "call_id", "severity", "created_at"},
            ),
            (
                "callback.created",
                webhook_events.callback_created_payload(callback),
                {"callback_id", "call_id", "contact_id", "requested_at", "created_at"},
            ),
            (
                "batch.completed",
                await webhook_events.batch_completed_payload(db, batch),
                {"batch_id", "status", "target_count", "final_status_histogram", "completed_at"},
            ),
            (
                "ping",
                webhook_events.ping_payload(uuid.uuid4()),
                {"endpoint_id"},
            ),
        ]

    for event, payload, expected_keys in table:
        assert set(payload.keys()) == ENVELOPE_KEYS, event
        assert payload["event"] == event
        assert set(payload["data"].keys()) == expected_keys, event


async def test_phi_exclusions_pinned(session_factory):
    phone = "+15550007777"
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="PHIPHI_NAME", phone_e164=phone, timezone="America/New_York"
        )
        contact_id = contact.id
        call = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
            idempotency_key="PHIPHI-IDEMKEY",
            livekit_room="PHIPHI-ROOM",
            dynamic_vars={"first_name": "PHIPHI_DYNVAR"},
        )
        call.end_reason = "PHIPHI_ENDREASON"
        call.sip_call_id = "PHIPHI-SIP"
        call.egress_id = "PHIPHI-EGRESS"
        call.recording_uri = "PHIPHI-URI"
        call.error = {"detail": "PHIPHI_ERRORDETAIL"}
        await db.flush()

        flag = await flags_repo.create_follow_up_flag(
            db,
            call_id=call.id,
            contact_id=contact_id,
            severity="urgent",
            category="medical",
            reason="PHIPHI_REASON",
        )
        callback = await callbacks_repo.create_callback_request(
            db,
            call_id=call.id,
            contact_id=contact_id,
            requested_time_text="PHIPHI_TIMETEXT",
            requested_at=None,
            notes="PHIPHI_NOTES",
        )
        batch = await batches_repo.create_batch_with_targets(
            db,
            targets=[BatchTargetIn(contact_id=contact_id)],
            **_batch_kwargs(name="PHIPHI_BATCHNAME", idempotency_key="PHIPHI-BKEY"),
        )

        payloads = {
            "call.started": await webhook_events.call_started_payload(db, call),
            "call.completed": await webhook_events.call_completed_payload(db, call),
            "flag.created": webhook_events.flag_created_payload(flag),
            "callback.created": webhook_events.callback_created_payload(callback),
            "batch.completed": await webhook_events.batch_completed_payload(db, batch),
        }

    sentinels = (
        "PHIPHI_NAME",  # contact name
        phone,  # contact phone_e164
        "PHIPHI_REASON",  # flag reason (free text)
        "medical",  # flag category (§6.4: dropped with contact_id)
        "PHIPHI_TIMETEXT",  # callback requested_time_text
        "PHIPHI_NOTES",  # callback notes
        "PHIPHI_ENDREASON",  # end_reason (conditionally free text)
        "PHIPHI_DYNVAR",  # dynamic_vars content
        "PHIPHI-ROOM",  # livekit_room
        "PHIPHI-URI",  # recording_uri
        "PHIPHI-SIP",  # sip_call_id
        "PHIPHI-EGRESS",  # egress_id
        "PHIPHI_ERRORDETAIL",  # error JSONB content
        "PHIPHI_BATCHNAME",  # batch name (PHI-free only by convention)
        "PHIPHI-IDEMKEY",  # raw call idempotency_key
        "PHIPHI-BKEY",  # raw batch idempotency_key
    )
    for event, payload in payloads.items():
        blob = json.dumps(payload)
        for sentinel in sentinels:
            assert sentinel not in blob, f"{event} leaked {sentinel!r}"

    # contact_id is excluded from flag.created specifically (§6.4: the health-domain
    # severity/category x person-identifier pairing); it stays on call.*/callback.*.
    assert str(contact_id) not in json.dumps(payloads["flag.created"])
    assert str(contact_id) in json.dumps(payloads["callback.created"])


async def test_origin_root_walk_retry_child(session_factory):
    contact_id, _phone = await _seed_contact(session_factory)
    batch_uuid = uuid.uuid4()
    async with session_factory() as db:
        root = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.NO_ANSWER,
            idempotency_key=f"batch:{batch_uuid}:3",
        )
        child = await calls_repo.schedule_retry(db, root.id)
        assert child is not None
        assert child.attempt == 2
        assert child.idempotency_key is None  # retry children carry no key of their own

        payload = await webhook_events.call_completed_payload(db, child)

    # §10.9: the builder walks parent_call_id to the root and parses ITS key —
    # origin describes the chain's origin on every attempt.
    assert payload["data"]["origin"] == {"source": "batch", "id": str(batch_uuid), "ordinal": 3}
    assert payload["data"]["attempt"] == 2
    assert payload["data"]["parent_call_id"] == str(root.id)


async def test_origin_null_for_operator_and_inbound(session_factory):
    contact_id, _phone = await _seed_contact(session_factory)
    async with session_factory() as db:
        oneoff = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
            idempotency_key="operator-key-1",
        )
        inbound = await calls_repo.create_inbound_call(
            db, contact_id=None, livekit_room=f"usan-inbound-{uuid.uuid4()}"
        )
        completed = await webhook_events.call_completed_payload(db, oneoff)
        started = await webhook_events.call_started_payload(db, inbound)

    assert completed["data"]["origin"] is None
    assert started["data"]["origin"] is None
    assert started["data"]["direction"] == "inbound"
    assert started["data"]["contact_id"] is None  # unknown inbound caller tolerated


async def test_call_completed_nulls_for_dnc_at_birth(session_factory):
    contact_id, _phone = await _seed_contact(session_factory)
    async with session_factory() as db:
        call = await calls_repo.create_call(
            db,
            contact_id=contact_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
        )
        payload = await webhook_events.call_completed_payload(db, call)

    data = payload["data"]
    assert data["status"] == "dnc_blocked"
    assert data["answered_at"] is None
    assert data["ended_at"] is None
    assert data["duration_seconds"] is None


async def test_batch_completed_counts(session_factory):
    contact_ids = [(await _seed_contact(session_factory))[0] for _ in range(3)]
    async with session_factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            targets=[BatchTargetIn(contact_id=eid) for eid in contact_ids],
            **_batch_kwargs(),
        )
        await db.commit()
        batch_id = batch.id

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        for target, final_status in zip(targets[:2], ("completed", "no_answer"), strict=True):
            await db.execute(
                update(CallBatchTarget)
                .where(CallBatchTarget.id == target.id)
                .values(status="done", final_status=final_status, finalized_at=NOW)
            )
        await db.execute(
            update(CallBatch)
            .where(CallBatch.id == batch_id)
            .values(status="completed", completed_at=NOW)
        )
        await db.commit()

    async with session_factory() as db:
        refreshed = await batches_repo.get_batch(db, batch_id)
        assert refreshed is not None
        expected_histogram = await batches_repo.final_status_histogram(db, batch_id)
        payload = await webhook_events.batch_completed_payload(db, refreshed)

    data = payload["data"]
    assert data["batch_id"] == str(batch_id)
    assert data["target_count"] == 3  # all targets, not just finalized ones
    assert data["final_status_histogram"] == expected_histogram
    assert expected_histogram == {"completed": 1, "no_answer": 1}
    assert data["status"] == "completed"
    assert datetime.fromisoformat(data["completed_at"]) == NOW
