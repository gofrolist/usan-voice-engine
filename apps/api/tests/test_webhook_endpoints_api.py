"""Operator API for /v1/webhook-endpoints + /v1/webhook-deliveries (spec §4).

Covers the full router contract: secret-once 201 create (never echoed again),
SSRF/closed-enum 422s, the 10-endpoint cap, breaker state + pending counts in
reads, the PATCH re-arm path, cascade delete, the flag-gated test-ping pipeline
(409 when WEBHOOK_DELIVERY_ENABLED=false — a test that can never send is a
lie), the paged/filtered deliveries surface, guarded redeliver semantics
(409/429 backpressure), sentinel-actor DB audit rows in the same commit, and
the operator-token gate on every method.

Flag-on tests follow plan executor note 6: setenv + ``get_settings.cache_clear()``
in the test body before the request — the lru_cache is re-read per request and
the ``client`` fixture's own teardown restores it.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import AdminAuditLog, WebhookDelivery, WebhookEndpoint
from usan_api.settings import get_settings

_URL = "https://hooks.example.com/sink"
_SENTINEL_ACTOR = "operator-api-key"


async def _run_db(async_database_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            result = await fn(db)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def _create(client, headers, *, url: str = _URL, events: list[str] | None = None) -> dict[str, Any]:
    r = client.post(
        "/v1/webhook-endpoints",
        json={"url": url, "events": events or ["call.completed"]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_delivery(
    db: AsyncSession,
    endpoint_id: str,
    *,
    event: str = "ping",
    status: str = "pending",
) -> uuid.UUID:
    row = WebhookDelivery(
        endpoint_id=uuid.UUID(endpoint_id),
        event=event,
        status=status,
        payload={"event": event, "occurred_at": "2026-06-10T00:00:00+00:00", "data": {}},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row.id


def test_create_201_returns_secret_exactly_once(client, operator_headers):
    body = _create(client, operator_headers)
    secret = body["secret"]
    assert len(secret) == 64
    int(secret, 16)  # parses as hex

    listed = client.get("/v1/webhook-endpoints", headers=operator_headers)
    assert listed.status_code == 200
    assert all("secret" not in item for item in listed.json())

    detail = client.get(f"/v1/webhook-endpoints/{body['id']}", headers=operator_headers)
    assert detail.status_code == 200
    assert "secret" not in detail.json()

    patched = client.patch(
        f"/v1/webhook-endpoints/{body['id']}",
        json={"description": "ops sink"},
        headers=operator_headers,
    )
    assert patched.status_code == 200
    assert "secret" not in patched.json()


def test_create_422_invalid_url_and_events(client, operator_headers):
    r = client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://metadata.google.internal/", "events": ["call.completed"]},
        headers=operator_headers,
    )
    assert r.status_code == 422

    r = client.post(
        "/v1/webhook-endpoints",
        json={"url": _URL, "events": ["ping"]},
        headers=operator_headers,
    )
    assert r.status_code == 422


def test_create_422_over_endpoint_cap(client, operator_headers):
    for n in range(10):
        _create(client, operator_headers, url=f"https://hooks.example.com/sink-{n}")
    r = client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://hooks.example.com/sink-10", "events": ["call.completed"]},
        headers=operator_headers,
    )
    assert r.status_code == 422


def test_list_includes_breaker_state_and_pending_count(
    client, operator_headers, async_database_url
):
    endpoint_id = _create(client, operator_headers)["id"]

    async def _seed(db: AsyncSession) -> None:
        for _ in range(3):
            await _seed_delivery(db, endpoint_id)
        await db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.id == uuid.UUID(endpoint_id))
            .values(consecutive_failures=4)
        )

    asyncio.run(_run_db(async_database_url, _seed))

    listed = client.get("/v1/webhook-endpoints", headers=operator_headers)
    assert listed.status_code == 200
    (item,) = listed.json()
    assert item["pending_deliveries"] == 3
    assert item["consecutive_failures"] == 4
    assert item["disabled_reason"] is None
    assert item["enabled"] is True


def test_patch_reenable_resets_breaker(client, operator_headers, async_database_url):
    endpoint_id = _create(client, operator_headers)["id"]

    async def _trip(db: AsyncSession) -> None:
        await db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.id == uuid.UUID(endpoint_id))
            .values(enabled=False, disabled_reason="circuit_breaker", consecutive_failures=10)
        )

    asyncio.run(_run_db(async_database_url, _trip))

    r = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"enabled": True}, headers=operator_headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["consecutive_failures"] == 0
    assert body["disabled_reason"] is None


def test_patch_url_revalidated(client, operator_headers):
    endpoint_id = _create(client, operator_headers)["id"]
    r = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}",
        json={"url": "https://localhost/"},
        headers=operator_headers,
    )
    assert r.status_code == 422


def test_delete_204_cascades_pending_backlog(client, operator_headers, async_database_url):
    endpoint_id = _create(client, operator_headers)["id"]

    async def _seed(db: AsyncSession) -> None:
        await _seed_delivery(db, endpoint_id)
        await _seed_delivery(db, endpoint_id, status="failed")

    asyncio.run(_run_db(async_database_url, _seed))

    r = client.delete(f"/v1/webhook-endpoints/{endpoint_id}", headers=operator_headers)
    assert r.status_code == 204

    async def _count(db: AsyncSession) -> int:
        result = await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(endpoint_id))
        )
        return len(list(result.scalars().all()))

    assert asyncio.run(_run_db(async_database_url, _count)) == 0
    assert (
        client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=operator_headers).status_code
        == 404
    )


def test_test_ping_409_when_delivery_disabled(client, operator_headers):
    # WEBHOOK_DELIVERY_ENABLED defaults to false — a test that can never send is a lie (§4).
    endpoint_id = _create(client, operator_headers)["id"]
    r = client.post(f"/v1/webhook-endpoints/{endpoint_id}/test", headers=operator_headers)
    assert r.status_code == 409


def test_test_ping_enqueues_real_pipeline_row(
    client, operator_headers, monkeypatch, async_database_url
):
    # Deterministic despite an always-on lifespan poller (plan executor note 9): the
    # background poller holds the flag-off lifespan-time settings snapshot, so the
    # per-test flag flip below affects requests only — the ping row stays pending.
    monkeypatch.setenv("WEBHOOK_DELIVERY_ENABLED", "true")
    get_settings.cache_clear()

    endpoint_id = _create(client, operator_headers)["id"]
    r = client.post(f"/v1/webhook-endpoints/{endpoint_id}/test", headers=operator_headers)
    assert r.status_code == 202
    delivery_id = r.json()["delivery_id"]

    async def _row(db: AsyncSession) -> tuple[WebhookDelivery, datetime]:
        row = await db.get(WebhookDelivery, uuid.UUID(delivery_id))
        assert row is not None
        db_now = (await db.execute(text("SELECT now()"))).scalar_one()
        return row, db_now

    row, db_now = asyncio.run(_run_db(async_database_url, _row))
    assert row.event == "ping"
    assert row.status == "pending"
    assert row.next_attempt_at <= db_now

    # Disabled endpoint -> 409 even with the flag on.
    r = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"enabled": False}, headers=operator_headers
    )
    assert r.status_code == 200
    r = client.post(f"/v1/webhook-endpoints/{endpoint_id}/test", headers=operator_headers)
    assert r.status_code == 409


def test_test_ping_429_over_pending_cap(client, operator_headers, monkeypatch, async_database_url):
    # The same pending-backlog backpressure redeliver has (§8.4): a test ping is
    # one more enqueue, so a leaked operator key must not be able to grow an
    # unbounded backlog through /test either.
    monkeypatch.setenv("WEBHOOK_DELIVERY_ENABLED", "true")
    get_settings.cache_clear()

    endpoint_id = _create(client, operator_headers)["id"]

    async def _backlog(db: AsyncSession) -> None:
        for _ in range(100):
            await _seed_delivery(db, endpoint_id)

    asyncio.run(_run_db(async_database_url, _backlog))

    r = client.post(f"/v1/webhook-endpoints/{endpoint_id}/test", headers=operator_headers)
    assert r.status_code == 429


def test_deliveries_list_paged_filtered(client, operator_headers, async_database_url):
    endpoint_id = _create(client, operator_headers)["id"]

    async def _seed(db: AsyncSession) -> list[str]:
        ids = [
            await _seed_delivery(db, endpoint_id, event="ping", status="pending"),
            await _seed_delivery(db, endpoint_id, event="ping", status="failed"),
            await _seed_delivery(db, endpoint_id, event="call.completed", status="delivered"),
        ]
        # Stagger created_at so newest-first ordering is observable.
        for offset_min, row_id in enumerate(ids):
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == row_id)
                .values(created_at=text(f"now() - interval '{30 - offset_min} minutes'"))
            )
        return [str(i) for i in ids]

    oldest, middle, newest = asyncio.run(_run_db(async_database_url, _seed))

    r = client.get(f"/v1/webhook-endpoints/{endpoint_id}/deliveries", headers=operator_headers)
    assert r.status_code == 200
    items = r.json()
    assert [item["id"] for item in items] == [newest, middle, oldest]
    assert all("updated_at" in item and "payload" in item for item in items)

    r = client.get(
        f"/v1/webhook-endpoints/{endpoint_id}/deliveries?status=failed", headers=operator_headers
    )
    assert [item["id"] for item in r.json()] == [middle]

    r = client.get(
        f"/v1/webhook-endpoints/{endpoint_id}/deliveries?event=ping", headers=operator_headers
    )
    assert [item["id"] for item in r.json()] == [middle, oldest]

    # limit clamped to 1..100: 0 floors to one row, an oversize limit doesn't error.
    r = client.get(
        f"/v1/webhook-endpoints/{endpoint_id}/deliveries?limit=0", headers=operator_headers
    )
    assert [item["id"] for item in r.json()] == [newest]
    r = client.get(
        f"/v1/webhook-endpoints/{endpoint_id}/deliveries?limit=500", headers=operator_headers
    )
    assert len(r.json()) == 3


def test_redeliver_semantics(client, operator_headers, async_database_url):
    endpoint_id = _create(client, operator_headers)["id"]

    async def _seed_failed(db: AsyncSession) -> str:
        return str(await _seed_delivery(db, endpoint_id, status="failed"))

    failed_id = asyncio.run(_run_db(async_database_url, _seed_failed))

    r = client.post(f"/v1/webhook-deliveries/{failed_id}/redeliver", headers=operator_headers)
    assert r.status_code == 202
    assert r.json()["delivery_id"] == failed_id

    async def _row(db: AsyncSession) -> tuple[WebhookDelivery, datetime]:
        row = await db.get(WebhookDelivery, uuid.UUID(failed_id))
        assert row is not None
        db_now = (await db.execute(text("SELECT now()"))).scalar_one()
        return row, db_now

    row, db_now = asyncio.run(_run_db(async_database_url, _row))
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_attempt_at <= db_now
    assert row.response_code is None
    assert row.last_error is None

    # Already pending -> 409.
    r = client.post(f"/v1/webhook-deliveries/{failed_id}/redeliver", headers=operator_headers)
    assert r.status_code == 409

    # Unknown id -> 404.
    r = client.post(f"/v1/webhook-deliveries/{uuid.uuid4()}/redeliver", headers=operator_headers)
    assert r.status_code == 404

    # Backpressure: >=100 pending rows on the endpoint -> 429 (§4/§8.4).
    async def _backlog(db: AsyncSession) -> str:
        for _ in range(99):  # one pending row already exists (the redelivered one)
            await _seed_delivery(db, endpoint_id)
        return str(await _seed_delivery(db, endpoint_id, status="failed"))

    failed_id_2 = asyncio.run(_run_db(async_database_url, _backlog))
    r = client.post(f"/v1/webhook-deliveries/{failed_id_2}/redeliver", headers=operator_headers)
    assert r.status_code == 429

    # Disabled endpoint -> 409 (before any reset happens).
    r = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"enabled": False}, headers=operator_headers
    )
    assert r.status_code == 200
    r = client.post(f"/v1/webhook-deliveries/{failed_id_2}/redeliver", headers=operator_headers)
    assert r.status_code == 409


def test_mutations_write_sentinel_audit_rows_same_commit(
    client, operator_headers, monkeypatch, async_database_url
):
    monkeypatch.setenv("WEBHOOK_DELIVERY_ENABLED", "true")
    get_settings.cache_clear()

    created = _create(client, operator_headers, url="https://hooks.example.com/audit-sink")
    endpoint_id, secret = created["id"], created["secret"]

    r = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}",
        json={"description": "ops sink"},
        headers=operator_headers,
    )
    assert r.status_code == 200
    r = client.post(f"/v1/webhook-endpoints/{endpoint_id}/test", headers=operator_headers)
    assert r.status_code == 202

    async def _seed_failed(db: AsyncSession) -> str:
        return str(await _seed_delivery(db, endpoint_id, status="failed"))

    failed_id = asyncio.run(_run_db(async_database_url, _seed_failed))
    r = client.post(f"/v1/webhook-deliveries/{failed_id}/redeliver", headers=operator_headers)
    assert r.status_code == 202
    r = client.delete(f"/v1/webhook-endpoints/{endpoint_id}", headers=operator_headers)
    assert r.status_code == 204

    async def _audit_rows(db: AsyncSession) -> list[AdminAuditLog]:
        result = await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))
        return list(result.scalars().all())

    rows = asyncio.run(_run_db(async_database_url, _audit_rows))
    actions = [row.action for row in rows]
    assert actions == [
        "webhook_endpoint_created",
        "webhook_endpoint_updated",
        "webhook_test_sent",
        "webhook_redelivered",
        "webhook_endpoint_deleted",
    ]
    assert all(row.actor_email == _SENTINEL_ACTOR for row in rows)

    by_action = {row.action: row for row in rows}
    created_row = by_action["webhook_endpoint_created"]
    assert created_row.entity_type == "webhook_endpoint"
    assert created_row.entity_id == endpoint_id
    assert created_row.detail == {"events": ["call.completed"]}
    assert by_action["webhook_endpoint_updated"].detail == {"changed": ["description"]}
    assert by_action["webhook_endpoint_updated"].entity_id == endpoint_id
    assert by_action["webhook_endpoint_deleted"].entity_id == endpoint_id
    assert by_action["webhook_redelivered"].entity_id == failed_id

    # Never the secret, never the URL — in any audit row's serialized detail (§4).
    for row in rows:
        serialized = json.dumps(row.detail)
        assert secret not in serialized
        assert "hooks.example.com" not in serialized


def test_requires_operator_token(client):
    some_id = str(uuid.uuid4())
    assert client.post("/v1/webhook-endpoints", json={}).status_code == 401
    assert client.get("/v1/webhook-endpoints").status_code == 401
    assert client.get(f"/v1/webhook-endpoints/{some_id}").status_code == 401
    assert client.patch(f"/v1/webhook-endpoints/{some_id}", json={}).status_code == 401
    assert client.delete(f"/v1/webhook-endpoints/{some_id}").status_code == 401
    assert client.post(f"/v1/webhook-endpoints/{some_id}/test").status_code == 401
    assert client.get(f"/v1/webhook-endpoints/{some_id}/deliveries").status_code == 401
    assert client.post(f"/v1/webhook-deliveries/{some_id}/redeliver").status_code == 401
