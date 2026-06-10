"""Operator API for /v1/batches (spec §4.2, §5.6).

Covers the full router contract: all-or-nothing create with per-target 422
detail, digest replay (200 same payload / 409 divergence — including replay
after cancel, which returns the cancelled batch and never re-runs), bounded
list + counts, detail with final_status histogram, guarded cancel semantics
(pending targets flipped, QUEUED chain tips cancelled, in-flight untouched,
chain-tip hop walk), ids-only audit logging (never the batch name, spec §8),
and the operator-token gate.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch
from usan_api.repositories import call_batches as batches_repo

_OP = {"Authorization": "Bearer " + "o" * 32}
NOW = datetime(2026, 6, 10, 16, 0, tzinfo=UTC)


def _seed_elder(client, *, name: str = "Rose Elder") -> str:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    r = client.post(
        "/v1/elders",
        json={"name": name, "phone_e164": phone, "timezone": "America/New_York"},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _batch_body(elder_ids: list[str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "June campaign",
        "targets": [{"elder_id": eid} for eid in elder_ids],
    }
    body.update(overrides)
    return body


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


async def _seed_call(
    db: AsyncSession,
    elder_id: str,
    *,
    status: CallStatus,
    scheduled_at: datetime | None = None,
    parent_call_id: uuid.UUID | None = None,
    attempt: int = 1,
) -> uuid.UUID:
    call = Call(
        elder_id=uuid.UUID(elder_id),
        direction=CallDirection.OUTBOUND,
        status=status,
        scheduled_at=scheduled_at,
        parent_call_id=parent_call_id,
        attempt=attempt,
        livekit_room=f"usan-outbound-{uuid.uuid4()}",
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    return call.id


async def _call_status(db: AsyncSession, call_id: uuid.UUID) -> CallStatus:
    call = await db.get(Call, call_id)
    assert call is not None
    return call.status


def test_create_batch_201_inserts_targets_one_txn(client):
    elder_ids = [_seed_elder(client) for _ in range(3)]
    r = client.post("/v1/batches", json=_batch_body(elder_ids), headers=_OP)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["counts"] == {
        "pending": 3,
        "materialized": 0,
        "done": 0,
        "skipped": 0,
        "cancelled": 0,
    }

    detail = client.get(f"/v1/batches/{body['id']}", headers=_OP)
    assert detail.status_code == 200
    targets = detail.json()["targets"]
    assert [t["target_index"] for t in targets] == [0, 1, 2]
    assert [t["elder_id"] for t in targets] == elder_ids
    assert all(t["status"] == "pending" for t in targets)


def test_create_batch_422_unknown_elder_with_target_detail(client):
    elder_ids = [_seed_elder(client), str(uuid.uuid4()), _seed_elder(client)]
    r = client.post("/v1/batches", json=_batch_body(elder_ids), headers=_OP)
    assert r.status_code == 422
    assert r.json()["detail"] == [{"target_index": 1, "error": "elder not found"}]
    # All-or-nothing: no batch row persisted.
    assert client.get("/v1/batches", headers=_OP).json() == []


def test_create_batch_422_per_target_vars_cap(client):
    elder_id = _seed_elder(client)
    body = _batch_body([elder_id])
    body["targets"][0]["dynamic_vars"] = {"pad": "x" * 9000}  # > 8 KB canonical cap
    r = client.post("/v1/batches", json=body, headers=_OP)
    assert r.status_code == 422


def test_create_batch_422_duplicate_elder(client):
    elder_id = _seed_elder(client)
    r = client.post("/v1/batches", json=_batch_body([elder_id, elder_id]), headers=_OP)
    assert r.status_code == 422


def test_create_batch_422_bad_profile_override_batch_and_target(client):
    elder_ids = [_seed_elder(client) for _ in range(2)]

    r_batch = client.post(
        "/v1/batches",
        json=_batch_body(elder_ids, profile_override=str(uuid.uuid4())),
        headers=_OP,
    )
    assert r_batch.status_code == 422
    assert any(item["target_index"] == "batch" for item in r_batch.json()["detail"])

    body = _batch_body(elder_ids)
    body["targets"][1]["profile_override"] = str(uuid.uuid4())
    r_target = client.post("/v1/batches", json=body, headers=_OP)
    assert r_target.status_code == 422
    assert any(item["target_index"] == 1 for item in r_target.json()["detail"])


def test_create_batch_replay_200_same_digest(client):
    elder_ids = [_seed_elder(client) for _ in range(2)]
    body = _batch_body(elder_ids, idempotency_key="june-batch-1")
    first = client.post("/v1/batches", json=body, headers=_OP)
    assert first.status_code == 201
    replay = client.post("/v1/batches", json=body, headers=_OP)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    # Still exactly one batch.
    assert len(client.get("/v1/batches", headers=_OP).json()) == 1


def test_create_batch_409_same_key_different_digest(client):
    elder_ids = [_seed_elder(client) for _ in range(2)]
    body = _batch_body(elder_ids, idempotency_key="june-batch-2")
    assert client.post("/v1/batches", json=body, headers=_OP).status_code == 201
    body["targets"][0]["dynamic_vars"] = {"first_name": "Rose"}  # one var changed
    r = client.post("/v1/batches", json=body, headers=_OP)
    assert r.status_code == 409


def test_create_batch_replay_after_cancel_returns_cancelled_batch(client):
    # Digest replay deliberately ignores batch status (spec §4.2/§9): the same
    # key + payload re-POSTed after cancel returns the CANCELLED batch — it must
    # never silently re-run the campaign.
    elder_ids = [_seed_elder(client) for _ in range(2)]
    body = _batch_body(elder_ids, idempotency_key="june-batch-3")
    created = client.post("/v1/batches", json=body, headers=_OP)
    assert created.status_code == 201
    batch_id = created.json()["id"]
    assert client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP).status_code == 200

    replay = client.post("/v1/batches", json=body, headers=_OP)
    assert replay.status_code == 200
    assert replay.json()["id"] == batch_id
    assert replay.json()["status"] == "cancelled"


def test_list_batches_counts_and_status_filter(client):
    first = client.post(
        "/v1/batches",
        json=_batch_body([_seed_elder(client) for _ in range(2)]),
        headers=_OP,
    ).json()
    second = client.post("/v1/batches", json=_batch_body([_seed_elder(client)]), headers=_OP).json()
    assert client.post(f"/v1/batches/{second['id']}/cancel", headers=_OP).status_code == 200

    r_all = client.get("/v1/batches", headers=_OP)
    assert r_all.status_code == 200
    rows = {row["id"]: row for row in r_all.json()}
    assert set(rows) == {first["id"], second["id"]}
    assert rows[first["id"]]["counts"]["pending"] == 2
    assert rows[second["id"]]["counts"]["cancelled"] == 1

    r_cancelled = client.get("/v1/batches", params={"status": "cancelled"}, headers=_OP)
    assert [row["id"] for row in r_cancelled.json()] == [second["id"]]


def test_get_batch_detail_histogram(client, async_database_url):
    elder_ids = [_seed_elder(client) for _ in range(2)]
    batch_id = client.post("/v1/batches", json=_batch_body(elder_ids), headers=_OP).json()["id"]

    async def _finalize_one(db: AsyncSession) -> None:
        targets = await batches_repo.list_targets(db, uuid.UUID(batch_id))
        call_id = await _seed_call(db, elder_ids[0], status=CallStatus.COMPLETED)
        assert await batches_repo.mark_target_materialized(db, targets[0], call_id=call_id, now=NOW)
        assert await batches_repo.finalize_target(db, targets[0], final_status="completed", now=NOW)

    asyncio.run(_run_db(async_database_url, _finalize_one))

    r = client.get(f"/v1/batches/{batch_id}", headers=_OP)
    assert r.status_code == 200
    body = r.json()
    assert body["final_status_histogram"] == {"completed": 1}
    assert body["counts"]["done"] == 1
    assert body["counts"]["pending"] == 1
    assert client.get(f"/v1/batches/{uuid.uuid4()}", headers=_OP).status_code == 404


def test_cancel_batch_200_marks_pending_and_queued_roots(client, async_database_url):
    elder_ids = [_seed_elder(client) for _ in range(3)]
    batch_id = client.post("/v1/batches", json=_batch_body(elder_ids), headers=_OP).json()["id"]

    async def _seed_states(db: AsyncSession) -> dict[str, uuid.UUID]:
        targets = await batches_repo.list_targets(db, uuid.UUID(batch_id))
        queued_root = await _seed_call(db, elder_ids[1], status=CallStatus.QUEUED, scheduled_at=NOW)
        assert await batches_repo.mark_target_materialized(
            db, targets[1], call_id=queued_root, now=NOW
        )
        in_progress_root = await _seed_call(db, elder_ids[2], status=CallStatus.IN_PROGRESS)
        assert await batches_repo.mark_target_materialized(
            db, targets[2], call_id=in_progress_root, now=NOW
        )
        return {"queued": queued_root, "in_progress": in_progress_root}

    roots = asyncio.run(_run_db(async_database_url, _seed_states))

    r = client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None
    # The pending target flipped; materialized targets stay for the finalizer.
    assert body["counts"] == {
        "pending": 0,
        "materialized": 2,
        "done": 0,
        "skipped": 0,
        "cancelled": 1,
    }

    async def _read_statuses(db: AsyncSession) -> tuple[CallStatus, CallStatus]:
        return (
            await _call_status(db, roots["queued"]),
            await _call_status(db, roots["in_progress"]),
        )

    queued_status, in_progress_status = asyncio.run(_run_db(async_database_url, _read_statuses))
    assert queued_status is CallStatus.CANCELLED  # first writer of the enum value
    assert in_progress_status is CallStatus.IN_PROGRESS  # in-flight finishes naturally (§5.6)


def test_cancel_batch_cancels_chain_tip_not_root(client, async_database_url):
    # A NO_ANSWER root with a QUEUED retry child: cancel must walk the chain via
    # parent_call_id and flip the TIP, leaving the root's truthful outcome alone.
    elder_id = _seed_elder(client)
    batch_id = client.post("/v1/batches", json=_batch_body([elder_id]), headers=_OP).json()["id"]

    async def _seed_chain(db: AsyncSession) -> dict[str, uuid.UUID]:
        targets = await batches_repo.list_targets(db, uuid.UUID(batch_id))
        root = await _seed_call(db, elder_id, status=CallStatus.NO_ANSWER)
        child = await _seed_call(
            db,
            elder_id,
            status=CallStatus.QUEUED,
            scheduled_at=NOW,
            parent_call_id=root,
            attempt=2,
        )
        assert await batches_repo.mark_target_materialized(db, targets[0], call_id=root, now=NOW)
        return {"root": root, "child": child}

    chain = asyncio.run(_run_db(async_database_url, _seed_chain))

    assert client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP).status_code == 200

    async def _read_statuses(db: AsyncSession) -> tuple[CallStatus, CallStatus]:
        return (
            await _call_status(db, chain["root"]),
            await _call_status(db, chain["child"]),
        )

    root_status, child_status = asyncio.run(_run_db(async_database_url, _read_statuses))
    assert child_status is CallStatus.CANCELLED  # the chain tip is what gets cancelled
    assert root_status is CallStatus.NO_ANSWER  # the root's outcome is never rewritten


def test_cancel_idempotent_200_and_completed_409(client, async_database_url):
    batch_id = client.post(
        "/v1/batches", json=_batch_body([_seed_elder(client)]), headers=_OP
    ).json()["id"]
    first = client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP)
    assert first.status_code == 200
    again = client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP)
    assert again.status_code == 200  # idempotent: unchanged cancelled batch
    assert again.json()["status"] == "cancelled"
    assert again.json()["cancelled_at"] == first.json()["cancelled_at"]

    completed_id = client.post(
        "/v1/batches", json=_batch_body([_seed_elder(client)]), headers=_OP
    ).json()["id"]

    async def _complete(db: AsyncSession) -> None:
        await db.execute(
            update(CallBatch)
            .where(CallBatch.id == uuid.UUID(completed_id))
            .values(status="completed", completed_at=NOW)
        )

    asyncio.run(_run_db(async_database_url, _complete))
    assert client.post(f"/v1/batches/{completed_id}/cancel", headers=_OP).status_code == 409

    assert client.post(f"/v1/batches/{uuid.uuid4()}/cancel", headers=_OP).status_code == 404


def test_batch_mutations_write_audit_log_lines(client):
    elder_id = _seed_elder(client)
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        batch_id = client.post("/v1/batches", json=_batch_body([elder_id]), headers=_OP).json()[
            "id"
        ]
        assert client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP).status_code == 200
    finally:
        logger.remove(handler_id)

    audit = [r for r in records if r["extra"].get("batch_id") == batch_id]
    actions = {r["extra"].get("action") for r in audit}
    assert {"batch_created", "batch_cancelled"} <= actions  # §9: every mutation audited
    for record in audit:
        assert record["extra"].get("client"), "audit line must bind the client IP"
        assert record["extra"].get("batch_id") == batch_id
        assert record["extra"].get("action")


def test_batch_log_lines_never_bind_name(client):
    # spec §8: call_batches.name is PHI-free BY CONVENTION ONLY — the log layer
    # must never bind it, so a name that does contain PHI never reaches the logs.
    elder_id = _seed_elder(client)
    batch_name = "Sentinel Sweep Confidential"
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        batch_id = client.post(
            "/v1/batches", json=_batch_body([elder_id], name=batch_name), headers=_OP
        ).json()["id"]
        assert client.post(f"/v1/batches/{batch_id}/cancel", headers=_OP).status_code == 200
    finally:
        logger.remove(handler_id)

    assert records, "expected at least the audit records"
    for record in records:
        assert "name" not in record["extra"]
        assert batch_name not in record["message"]
        assert all(batch_name not in str(v) for v in record["extra"].values())


def test_batches_require_operator_token(client):
    batch_id = uuid.uuid4()
    assert client.post("/v1/batches", json={}).status_code == 401
    assert client.get("/v1/batches").status_code == 401
    assert client.get(f"/v1/batches/{batch_id}").status_code == 401
    assert client.post(f"/v1/batches/{batch_id}/cancel").status_code == 401
