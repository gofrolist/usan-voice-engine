"""Delivery worker: signed POSTs, grouping, breaker, housekeeping cycle (Task E2).

Pins spec §5.3 per-row delivery (sign-what-you-send wire format, guarded
outcomes, per-row commits), §5.2 per-endpoint concurrent groups, §5.5 breaker
trip + mid-cycle group stop, §5.1's flag-gates-delivery-only contract, and the
§5.4 hourly housekeeping cadence. HTTP is faked via ``httpx.MockTransport``
through the ``_build_client`` seam; DNS via the ``ssrf_guard._resolve`` seam
(public addresses by default). Settings are constructed directly — both
``poll_once`` and ``deliver_one`` take the settings instance explicitly, so no
env/``get_settings`` plumbing is involved.
"""

import asyncio
import hashlib
import hmac
import json
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from loguru import logger
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import ssrf_guard, webhook_delivery
from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.settings import Settings

SECRET = "a" * 64
# PHIPHI sentinel in the query token: any leak of the URL into rows/logs is
# caught by substring assertions below.
URL = "https://hooks.example.com/sink?token=PHIPHI"

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
    """Delivery-enabled settings unless a test states otherwise."""
    return Settings(**{**_BASE, "WEBHOOK_DELIVERY_ENABLED": "true", **overrides})


# Spec §7's documented receiver snippet, embedded VERBATIM: the wire test below
# must verify with exactly the code we hand to operators.
def verify_usan_signature(
    secret: str, header: str, raw_body: bytes, tolerance_s: int = 300
) -> bool:
    parts = dict(kv.split("=", 1) for kv in header.split(","))
    ts_ms = int(parts["v"])
    if abs(time.time() * 1000 - ts_ms) > tolerance_s * 1000:
        return False  # replay window exceeded
    expected = hmac.new(
        secret.encode(), f"{ts_ms}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts["d"])


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Wraps the test (before AND after): leftover webhook_endpoints rows change
    # enqueue fan-out behavior in unrelated modules (the C4-instrumented mutators).
    async def _wipe():
        async with session_factory() as db:
            await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
            await db.commit()

    await _wipe()
    yield
    await _wipe()


@pytest.fixture(autouse=True)
def _public_resolve(monkeypatch):
    # Delivery-time SSRF gate resolves to a public address by default; the
    # DNS-failure and SSRF tests override this with their own monkeypatch.
    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake)


def _install_client(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        webhook_delivery,
        "_build_client",
        lambda settings: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _seed_endpoint(
    factory,
    *,
    url: str = URL,
    failures: int = 0,
    events: list[str] | None = None,
) -> uuid.UUID:
    async with factory() as db:
        endpoint = WebhookEndpoint(
            url=url,
            secret=SECRET,
            events=events or ["call.completed"],
            consecutive_failures=failures,
        )
        db.add(endpoint)
        await db.commit()
        return endpoint.id


async def _seed_delivery(
    factory,
    endpoint_id: uuid.UUID,
    *,
    event: str = "call.completed",
    attempts: int = 0,
    due_offset: timedelta = timedelta(seconds=0),
) -> uuid.UUID:
    async with factory() as db:
        row = WebhookDelivery(
            endpoint_id=endpoint_id,
            event=event,
            payload=PAYLOAD,
            attempts=attempts,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=5) + due_offset,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _set_row(factory, delivery_id: uuid.UUID, **cols: object) -> None:
    async with factory() as db:
        await db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**cols)
        )
        await db.commit()


async def _get_delivery(factory, delivery_id: uuid.UUID) -> WebhookDelivery:
    async with factory() as db:
        row = await db.get(WebhookDelivery, delivery_id)
        assert row is not None
        return row


async def _get_endpoint(factory, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    async with factory() as db:
        row = await db.get(WebhookEndpoint, endpoint_id)
        assert row is not None
        return row


async def test_build_client_pins_timeout_and_no_redirects():
    # The production client config (spec §5.3/§8.2): every other test swaps
    # _build_client for a MockTransport, so without this pin the settings
    # timeout and the load-bearing follow_redirects=False could silently rot.
    client = webhook_delivery._build_client(_settings(WEBHOOK_DELIVERY_TIMEOUT_S="7"))
    try:
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(7)
    finally:
        await client.aclose()


async def test_delivers_signed_post_2xx(session_factory, monkeypatch):
    # consecutive_failures=5 makes the reset assertion non-vacuous: with a
    # zero-seeded endpoint the reset_failures wiring could be deleted and a
    # 0 -> 0 check would still pass.
    endpoint_id = await _seed_endpoint(session_factory, failures=5)
    d_id = await _seed_delivery(session_factory, endpoint_id)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["delivered"] == 1

    row = await _get_delivery(session_factory, d_id)
    assert row.status == "delivered"
    assert row.response_code == 200
    assert row.delivered_at is not None

    # Success resets the breaker: 5 -> 0 (spec §5.5).
    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.consecutive_failures == 0

    # The wire (spec §7): headers + signature over the exact raw bytes sent.
    (request,) = seen
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"] == "usan-voice-engine-webhooks/1.0"
    assert request.headers["X-Usan-Event"] == "call.completed"
    assert request.headers["X-Usan-Delivery-Id"] == str(d_id)
    raw = request.content
    assert verify_usan_signature(SECRET, request.headers["X-Usan-Signature"], raw) is True
    # The signed body carries the dedupe key (spec §6.1/§10.5) + the stored payload.
    body = json.loads(raw)
    assert body["delivery_id"] == str(d_id)
    assert body["event"] == "call.completed"
    assert body["data"] == PAYLOAD["data"]


async def test_non_2xx_schedules_retry(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id)
    _install_client(monkeypatch, lambda request: httpx.Response(500))

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["retry_scheduled"] == 1

    row = await _get_delivery(session_factory, d_id)
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.response_code == 500
    assert row.last_error == "HTTPStatusError"

    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.consecutive_failures == 1


async def test_3xx_is_failure_never_followed(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://internal.example.com/"})

    _install_client(monkeypatch, handler)

    await webhook_delivery.poll_once(session_factory, _settings())

    # follow_redirects=False (spec §8.2): exactly one request, never the Location.
    assert len(seen) == 1
    row = await _get_delivery(session_factory, d_id)
    assert row.status == "pending"
    assert row.response_code == 302
    assert row.last_error == "HTTPStatusError"


async def test_timeout_and_transport_errors_recorded_as_type_names(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id)

    def handler(request: httpx.Request) -> httpx.Response:
        # Exception text deliberately embeds the URL + sentinel — neither may
        # survive into the row (type-name-only rule, spec §5.3/§8.3).
        raise httpx.ConnectTimeout(f"SENTINEL-EXC-TEXT timeout connecting to {URL}")

    _install_client(monkeypatch, handler)

    await webhook_delivery.poll_once(session_factory, _settings())

    row = await _get_delivery(session_factory, d_id)
    assert row.status == "pending"
    assert row.response_code is None
    assert row.last_error == "ConnectTimeout"
    assert "SENTINEL-EXC-TEXT" not in (row.last_error or "")
    assert "PHIPHI" not in (row.last_error or "")
    assert "hooks.example.com" not in (row.last_error or "")


@pytest.mark.parametrize(
    ("exc_factory", "expected_name"),
    [
        (lambda: socket.gaierror(8, "nodename nor servname provided"), "gaierror"),
        (lambda: OSError("network unreachable"), "OSError"),
    ],
)
async def test_dns_failure_no_post_feeds_breaker(
    session_factory, monkeypatch, exc_factory, expected_name
):
    # NXDOMAIN — the most common dead-receiver mode — must not escape
    # deliver_one's except tuple, abort the endpoint group mid-gather, and
    # rot as crash_residual (plan executor note 4).
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id)

    async def _raise(host: str) -> list[str]:
        raise exc_factory()

    monkeypatch.setattr(ssrf_guard, "_resolve", _raise)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["retry_scheduled"] == 1

    assert seen == []  # the POST never happened
    row = await _get_delivery(session_factory, d_id)
    assert row.status == "pending"  # re-offered at the bumped lease rung
    assert row.attempts == 1
    assert row.last_error == expected_name
    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.consecutive_failures == 1


async def test_terminal_attempt_marks_failed(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id, attempts=3)  # claim -> 4
    _install_client(monkeypatch, lambda request: httpx.Response(500))

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["failed"] == 1

    row = await _get_delivery(session_factory, d_id)
    assert row.status == "failed"
    assert row.attempts == 4
    assert row.last_error == "HTTPStatusError"


async def test_ssrf_block_no_post_feeds_breaker(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id)

    async def _private(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _private)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["ssrf_blocked"] == 1

    assert seen == []  # rejected before any POST (spec §8.2/§10.4)
    row = await _get_delivery(session_factory, d_id)
    assert row.status == "pending"
    assert row.last_error == "SsrfBlocked"
    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.consecutive_failures == 1


async def test_terminal_ssrf_row_failed_with_type_name(session_factory, monkeypatch):
    # The row-level half of the §5.3 alert-honesty rule: the terminal attempt
    # flips to failed even when the failure mode is SSRF, and the diagnostic
    # last_error='SsrfBlocked' is preserved. (The metric-label half lands in F1.)
    endpoint_id = await _seed_endpoint(session_factory)
    d_id = await _seed_delivery(session_factory, endpoint_id, attempts=3)  # claim -> 4

    async def _private(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _private)
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["failed"] == 1

    row = await _get_delivery(session_factory, d_id)
    assert row.status == "failed"
    assert row.last_error == "SsrfBlocked"


async def test_breaker_trips_once_at_threshold_and_stops_group(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    ids = [await _seed_delivery(session_factory, endpoint_id) for _ in range(3)]
    # Deterministic in-group order: ids[0] is the oldest due.
    base = datetime.now(UTC) - timedelta(minutes=10)
    for i, d_id in enumerate(ids):
        await _set_row(session_factory, d_id, next_attempt_at=base + timedelta(seconds=i))

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    _install_client(monkeypatch, handler)

    records: list[Any] = []
    handler_id = logger.add(records.append, level=0)
    try:
        await webhook_delivery.poll_once(
            session_factory, _settings(WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD="1")
        )
    finally:
        logger.remove(handler_id)

    # Exactly one POST: the first failure trips the breaker (threshold=1) and
    # the per-row enabled re-check stops the group mid-cycle (§5.3 step 1/§5.5).
    assert len(seen) == 1

    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.enabled is False
    assert endpoint.disabled_reason == "circuit_breaker"

    first = await _get_delivery(session_factory, ids[0])
    assert first.last_error == "HTTPStatusError"
    for d_id in ids[1:]:
        row = await _get_delivery(session_factory, d_id)
        assert row.status == "pending"  # skipped, no outcome written
        assert row.last_error is None

    warns = [m for m in records if m.record["level"].name == "WARNING"]
    assert len(warns) == 1
    assert warns[0].record["extra"]["endpoint_id"] == str(endpoint_id)
    for message in records:
        assert "hooks.example.com" not in str(message)
        assert "PHIPHI" not in str(message)


async def test_groups_deliver_concurrently_ordered_within(session_factory, monkeypatch):
    endpoint_a = await _seed_endpoint(session_factory, url="https://a.example.com/hook")
    endpoint_b = await _seed_endpoint(session_factory, url="https://b.example.com/hook")
    a1 = await _seed_delivery(session_factory, endpoint_a)
    a2 = await _seed_delivery(session_factory, endpoint_a)
    b1 = await _seed_delivery(session_factory, endpoint_b)
    base = datetime.now(UTC) - timedelta(minutes=10)
    await _set_row(session_factory, a1, next_attempt_at=base)
    await _set_row(session_factory, a2, next_attempt_at=base + timedelta(seconds=1))
    await _set_row(session_factory, b1, next_attempt_at=base + timedelta(seconds=2))

    order: list[str] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.headers["X-Usan-Delivery-Id"])
        if request.url.host == "b.example.com":
            gate.set()
        elif request.headers["X-Usan-Delivery-Id"] == str(a1):
            # A's first delivery parks until B's lands: only concurrent
            # per-endpoint groups (asyncio.gather, §5.2) can finish the cycle.
            await asyncio.wait_for(gate.wait(), timeout=5)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    # A sequential-delivery regression FAILS here instead of hanging the suite.
    stats = await asyncio.wait_for(
        webhook_delivery.poll_once(session_factory, _settings()), timeout=5
    )
    assert stats["delivered"] == 3
    # Oldest-first WITHIN a group (per-endpoint ordering preserved).
    assert order.index(str(a1)) < order.index(str(a2))


async def test_per_row_outcome_commits(session_factory, monkeypatch):
    endpoint_id = await _seed_endpoint(session_factory)
    r1 = await _seed_delivery(session_factory, endpoint_id)
    r2 = await _seed_delivery(session_factory, endpoint_id)
    base = datetime.now(UTC) - timedelta(minutes=10)
    await _set_row(session_factory, r1, next_attempt_at=base)
    await _set_row(session_factory, r2, next_attempt_at=base + timedelta(seconds=1))

    # The second request probes from a FRESH session that the first row's
    # outcome is already committed — per-row commits, not a cycle-end batch
    # commit, bound the at-least-once duplicate window to one row (§5.3).
    probe: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["X-Usan-Delivery-Id"] == str(r2):
            async with session_factory() as db:
                row = await db.get(WebhookDelivery, r1)
                assert row is not None
                probe.append(row.status)
            return httpx.Response(500)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    await webhook_delivery.poll_once(session_factory, _settings())

    assert probe == ["delivered"]  # r1 was committed before r2's POST
    assert (await _get_delivery(session_factory, r1)).status == "delivered"
    assert (await _get_delivery(session_factory, r2)).status == "pending"


async def test_pending_count_computed_every_cycle(session_factory, monkeypatch):
    # Spec §9: backlog visibility is per-cycle, not hourly — the count is taken
    # at cycle start even with housekeeping off. (The gauge .set() lands in F1.)
    endpoint_id = await _seed_endpoint(session_factory)
    for _ in range(3):
        await _seed_delivery(session_factory, endpoint_id)
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    stats = await webhook_delivery.poll_once(session_factory, _settings(), run_housekeeping=False)
    assert stats["pending"] == 3


async def test_flag_off_no_claims_but_housekeeping_runs(session_factory, monkeypatch):
    # WEBHOOK_DELIVERY_ENABLED=false gates DELIVERY only (spec §5.1/§10.12):
    # nothing is claimed and nothing egresses, but the always-on housekeeping
    # half still expires the documented flag-off backlog.
    endpoint_id = await _seed_endpoint(session_factory)
    fresh_id = await _seed_delivery(session_factory, endpoint_id)
    stale_id = await _seed_delivery(session_factory, endpoint_id)
    await _set_row(session_factory, stale_id, created_at=datetime.now(UTC) - timedelta(days=8))

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)

    stats = await webhook_delivery.poll_once(
        session_factory, Settings(**_BASE), run_housekeeping=True
    )

    assert seen == []
    fresh = await _get_delivery(session_factory, fresh_id)
    assert fresh.status == "pending"
    assert fresh.attempts == 0  # never claimed
    stale = await _get_delivery(session_factory, stale_id)
    assert stale.status == "failed"
    assert stale.last_error == "expired"
    assert stats["expired"] == 1


async def test_housekeeping_skipped_unless_requested(session_factory, monkeypatch):
    # Sweep/expire/prune are hourly (the pending count is NOT — see the
    # per-cycle test above): run_housekeeping=False leaves the 8-day row alone.
    endpoint_id = await _seed_endpoint(session_factory)
    stale_id = await _seed_delivery(session_factory, endpoint_id)
    await _set_row(session_factory, stale_id, created_at=datetime.now(UTC) - timedelta(days=8))
    _install_client(monkeypatch, lambda request: httpx.Response(200))

    stats = await webhook_delivery.poll_once(
        session_factory, Settings(**_BASE), run_housekeeping=False
    )

    assert (await _get_delivery(session_factory, stale_id)).status == "pending"
    assert stats["expired"] == 0


def test_housekeeping_due_helper():
    # The §10.10 hourly-cadence pin, as a pure function.
    t = 1000.0
    assert webhook_delivery._housekeeping_due(None, t) is True  # first cycle always
    assert webhook_delivery._housekeeping_due(t, t + 3599.0) is False
    assert webhook_delivery._housekeeping_due(t, t + 3600.0) is True
