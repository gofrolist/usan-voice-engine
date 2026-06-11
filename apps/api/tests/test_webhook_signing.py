"""webhook_signing: pinned HMAC vectors, canonical bytes, receiver round-trip (spec §7/§10.6)."""

import hashlib
import hmac
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.webhook_signing import canonical_bytes, generate_secret, sign, signature_header

# Pinned vector (plan Task B3): any change to canonicalization or signing breaks this.
PINNED_SECRET = "a" * 64
PINNED_TS_MS = 1765432100000
PINNED_BODY = {
    "event": "ping",
    "occurred_at": "2026-06-10T00:00:00Z",
    "data": {"endpoint_id": "00000000-0000-0000-0000-000000000002"},
    "delivery_id": "00000000-0000-0000-0000-000000000001",
}
PINNED_CANONICAL = (
    b'{"data":{"endpoint_id":"00000000-0000-0000-0000-000000000002"},'
    b'"delivery_id":"00000000-0000-0000-0000-000000000001",'
    b'"event":"ping","occurred_at":"2026-06-10T00:00:00Z"}'
)
PINNED_DIGEST = "966b62c7ee18db2debabfeebccb8f943e5d78fd07115d32da75680a50254bd36"


# Spec §7's documented receiver snippet, embedded VERBATIM (imports hoisted to the
# top of this module per ruff E401): if the implementation drifts from what the
# docs tell receivers to run, this test breaks.
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


def test_sign_pinned_vector() -> None:
    raw = canonical_bytes(PINNED_BODY)
    assert raw == PINNED_CANONICAL
    assert sign(PINNED_SECRET, PINNED_TS_MS, raw) == PINNED_DIGEST


def test_documented_verify_snippet_round_trips() -> None:
    raw = canonical_bytes(PINNED_BODY)
    ts_ms = int(time.time() * 1000)
    header = signature_header(ts_ms, sign(PINNED_SECRET, ts_ms, raw))
    assert verify_usan_signature(PINNED_SECRET, header, raw) is True

    # Tampered body (one byte) -> rejected.
    tampered = bytes([raw[0] ^ 0x01]) + raw[1:]
    assert verify_usan_signature(PINNED_SECRET, header, tampered) is False

    # Replay tolerance boundary: 301 s in the past -> rejected, 299 s -> accepted.
    stale_ts = int(time.time() * 1000) - 301_000
    stale_header = signature_header(stale_ts, sign(PINNED_SECRET, stale_ts, raw))
    assert verify_usan_signature(PINNED_SECRET, stale_header, raw) is False

    fresh_ts = int(time.time() * 1000) - 299_000
    fresh_header = signature_header(fresh_ts, sign(PINNED_SECRET, fresh_ts, raw))
    assert verify_usan_signature(PINNED_SECRET, fresh_header, raw) is True


def test_canonical_bytes_invariant_under_key_reorder() -> None:
    a = {"event": "ping", "data": {"x": 1, "y": 2}, "occurred_at": "2026-06-10T00:00:00Z"}
    b = {"occurred_at": "2026-06-10T00:00:00Z", "data": {"y": 2, "x": 1}, "event": "ping"}
    assert canonical_bytes(a) == canonical_bytes(b)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def _truncate_webhooks(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
        await db.commit()


@pytest.mark.usefixtures("_truncate_webhooks")
async def test_canonical_bytes_survives_jsonb_round_trip(session_factory) -> None:
    """Signed bytes == sent bytes after JSONB storage (spec §10.6).

    JSONB does not preserve key order or whitespace; canonical_bytes must yield
    the same bytes for the stored payload as for the original dict, with the
    send-time delivery_id injection applied to both.
    """
    payload = {
        "event": "call.completed",
        "occurred_at": "2026-06-10T00:00:00Z",
        "data": {"zeta": 1, "alpha": "x", "nested": {"b": 2, "a": 1}},
    }
    async with session_factory() as db:
        endpoint = WebhookEndpoint(
            url="https://hooks.example.com/sink",
            secret="b" * 64,
            events=["call.completed"],
        )
        db.add(endpoint)
        await db.flush()
        delivery = WebhookDelivery(endpoint_id=endpoint.id, event="call.completed", payload=payload)
        db.add(delivery)
        await db.flush()
        delivery_id = str(delivery.id)
        await db.commit()

    expected = canonical_bytes(payload | {"delivery_id": delivery_id})

    async with session_factory() as db:
        row = (await db.execute(select(WebhookDelivery))).scalar_one()
        read_back = row.payload
        assert canonical_bytes(read_back | {"delivery_id": str(row.id)}) == expected


def test_generate_secret_is_64_hex_and_unique() -> None:
    s1 = generate_secret()
    s2 = generate_secret()
    assert len(s1) == 64
    int(s1, 16)  # parses as hex
    assert s1 != s2
