"""Webhook delivery metrics: counters + pending gauge (Task F1, spec §9).

Pins the closed outcome label set (``delivered|retry_scheduled|failed|
ssrf_blocked`` — "skipped" is a breaker no-attempt internal string, never a
label), the one-shot breaker-trip counter, the §5.3 alert-honesty rule
(terminal SSRF reads ``outcome="failed"``), and the per-cycle (NOT hourly)
pending-backlog gauge. Counters are process-global, so every assertion is
delta-based. HTTP/DNS fakes reuse the E2 seams (``_build_client`` +
``ssrf_guard._resolve``).
"""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import counter_value, gauge_value
from usan_api import ssrf_guard, webhook_delivery
from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.observability.custom_metrics import (
    WEBHOOK_DELIVERIES_TOTAL,
    WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL,
    WEBHOOK_PENDING_DELIVERIES,
)
from usan_api.settings import Settings

SECRET = "a" * 64
URL = "https://hooks.example.com/sink"

PAYLOAD: dict[str, Any] = {
    "event": "call.completed",
    "occurred_at": "2026-06-10T12:00:00+00:00",
    "data": {"call_id": "00000000-0000-0000-0000-00000000c411"},
}

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _settings(**overrides: str) -> Settings:
    return Settings(**{**_BASE, "WEBHOOK_DELIVERY_ENABLED": "true", **overrides})


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async def _wipe():
        async with session_factory() as db:
            await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
            await db.commit()

    await _wipe()
    yield
    await _wipe()


@pytest.fixture(autouse=True)
def _public_resolve(monkeypatch):
    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake)


def _install_client(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        webhook_delivery,
        "_build_client",
        lambda settings: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _seed_endpoint(factory, *, events: list[str] | None = None) -> uuid.UUID:
    async with factory() as db:
        endpoint = WebhookEndpoint(url=URL, secret=SECRET, events=events or ["call.completed"])
        db.add(endpoint)
        await db.commit()
        return endpoint.id


async def _seed_delivery(
    factory,
    endpoint_id: uuid.UUID,
    *,
    event: str = "call.completed",
    attempts: int = 0,
) -> uuid.UUID:
    async with factory() as db:
        row = WebhookDelivery(
            endpoint_id=endpoint_id,
            event=event,
            payload=PAYLOAD,
            attempts=attempts,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(row)
        await db.commit()
        return row.id


def _all_delivery_samples() -> dict[tuple[str, str], float]:
    """Every (event, outcome) sample of the deliveries counter — the
    whole-registry view the skipped-is-never-a-label assertions need."""
    out: dict[tuple[str, str], float] = {}
    for metric in WEBHOOK_DELIVERIES_TOTAL.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                out[(sample.labels["event"], sample.labels["outcome"])] = sample.value
    return out


async def test_delivered_increments_counter(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    await _seed_delivery(session_factory, endpoint_id, event="ping")
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    before = counter_value(WEBHOOK_DELIVERIES_TOTAL, event="ping", outcome="delivered")
    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["delivered"] == 1
    assert counter_value(WEBHOOK_DELIVERIES_TOTAL, event="ping", outcome="delivered") == before + 1


async def test_retry_and_terminal_outcome_labels(session_factory, monkeypatch):
    # One row driven down the full §5.2 ladder via time-travelled `now`:
    # attempts 1-3 are retry_scheduled, the 4th (terminal) is failed.
    endpoint_id = await _seed_endpoint(session_factory)
    await _seed_delivery(session_factory, endpoint_id)
    _install_client(monkeypatch, lambda request: httpx.Response(500))

    before_retry = counter_value(
        WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="retry_scheduled"
    )
    before_failed = counter_value(
        WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="failed"
    )

    t0 = datetime.now(UTC)
    # Past each ladder rung (+1m, +5m, +30m after attempts 1/2/3).
    for offset in (timedelta(0), timedelta(minutes=2), timedelta(minutes=10)):
        stats = await webhook_delivery.poll_once(session_factory, _settings(), now=t0 + offset)
        assert stats["retry_scheduled"] == 1
    assert (
        counter_value(WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="retry_scheduled")
        == before_retry + 3
    )

    stats = await webhook_delivery.poll_once(
        session_factory, _settings(), now=t0 + timedelta(minutes=90)
    )
    assert stats["failed"] == 1
    assert (
        counter_value(WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="failed")
        == before_failed + 1
    )


async def test_dns_failure_outcome_label(session_factory, monkeypatch):
    # DNS failures stay inside the contracted label set: outcome is
    # retry_scheduled; the 'gaierror' type name lives only in last_error.
    endpoint_id = await _seed_endpoint(session_factory)
    await _seed_delivery(session_factory, endpoint_id)

    async def _raise(host: str) -> list[str]:
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(ssrf_guard, "_resolve", _raise)
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    before = counter_value(
        WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="retry_scheduled"
    )
    before_samples = _all_delivery_samples()

    await webhook_delivery.poll_once(session_factory, _settings())

    assert (
        counter_value(WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="retry_scheduled")
        == before + 1
    )
    # No 'gaierror' (or any other off-set string) appeared as an outcome label.
    new_labels = set(_all_delivery_samples()) - set(before_samples)
    assert {outcome for _, outcome in new_labels} <= {"retry_scheduled"}


async def test_ssrf_outcome_label_rule(session_factory, monkeypatch):
    # §5.3 alert honesty: non-terminal SSRF block reads ssrf_blocked, but the
    # TERMINAL attempt reads failed — a permanently-private endpoint must still
    # reach the delivery-failed alert. (E2 pinned the row state; this is the
    # metric-label half.)
    endpoint_a = await _seed_endpoint(session_factory)
    endpoint_b = await _seed_endpoint(session_factory)
    await _seed_delivery(session_factory, endpoint_a)  # non-terminal (claim -> 1)
    await _seed_delivery(session_factory, endpoint_b, attempts=3)  # terminal (claim -> 4)

    async def _private(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _private)
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    before_ssrf = counter_value(
        WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="ssrf_blocked"
    )
    before_failed = counter_value(
        WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="failed"
    )

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["ssrf_blocked"] == 1
    assert stats["failed"] == 1

    assert (
        counter_value(WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="ssrf_blocked")
        == before_ssrf + 1
    )
    assert (
        counter_value(WEBHOOK_DELIVERIES_TOTAL, event="call.completed", outcome="failed")
        == before_failed + 1
    )


async def test_breaker_trip_metric_exactly_once_and_skipped_uncounted(session_factory, monkeypatch):
    # Threshold 1: the first failure trips the breaker (guarded-UPDATE one-shot)
    # and the remaining two rows skip without an attempt. The spec §9 outcome
    # set is CLOSED — "skipped" is a no-attempt internal string, never a label,
    # and skipped rows increment nothing.
    endpoint_id = await _seed_endpoint(session_factory)
    ids = [await _seed_delivery(session_factory, endpoint_id) for _ in range(3)]
    base = datetime.now(UTC) - timedelta(minutes=10)
    async with session_factory() as db:
        for i, d_id in enumerate(ids):
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == d_id)
                .values(next_attempt_at=base + timedelta(seconds=i))
            )
        await db.commit()

    _install_client(monkeypatch, lambda request: httpx.Response(500))

    before_disabled = counter_value(WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL)
    before_samples = _all_delivery_samples()

    stats = await webhook_delivery.poll_once(
        session_factory, _settings(WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD="1")
    )
    assert stats["retry_scheduled"] == 1
    assert stats["skipped"] >= 1  # group stopped mid-cycle

    # Exactly one trip increment (the guarded UPDATE fired once).
    assert counter_value(WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL) == before_disabled + 1

    after_samples = _all_delivery_samples()
    # The deliveries counter gained exactly ONE attempt total: the tripping
    # failure. The two breaker-skipped rows incremented nothing.
    assert sum(after_samples.values()) - sum(before_samples.values()) == 1
    assert (
        after_samples[("call.completed", "retry_scheduled")]
        - before_samples.get(("call.completed", "retry_scheduled"), 0.0)
        == 1
    )
    # No outcome="skipped" sample exists ANYWHERE in the registry.
    assert all(outcome != "skipped" for _, outcome in after_samples)


async def test_pending_gauge_set_every_cycle_even_flag_off(session_factory, monkeypatch):
    # Spec §9/§5.1: the backlog gauge is per-cycle, NOT hourly, and flag-
    # independent — run_housekeeping=False + WEBHOOK_DELIVERY_ENABLED=false
    # must still publish the depth (pre-enable observability).
    endpoint_id = await _seed_endpoint(session_factory)
    for _ in range(3):
        await _seed_delivery(session_factory, endpoint_id)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    stats = await webhook_delivery.poll_once(
        session_factory, Settings(**_BASE), run_housekeeping=False
    )

    assert seen == []  # flag off: nothing claimed, nothing egressed
    assert stats["pending"] == 3
    assert gauge_value(WEBHOOK_PENDING_DELIVERIES) == 3.0
