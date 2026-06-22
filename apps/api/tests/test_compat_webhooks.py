"""T026 — compat (RetellAI) call-event webhook integration.

End-to-end: register an agent webhook subscription on an allow-listed host -> drive a call
through started/ended/analyzed transitions -> the native lifecycle hooks fan compat
``{event, call_id}`` deliveries into the compat outbox -> the compat poller assembles the
full ``{event, call}`` body, signs the Retell scheme with the subscription's dedicated
secret, and POSTs it (mocked) with a stable ``x-retell-delivery-id``. Plus the PHI allow-list
gate at registration (off-list + empty-list both rejected) and delivery-time SSRF.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import ssrf_guard
from usan_api.compat import webhook_delivery as cwd
from usan_api.compat.errors import CompatError
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import AgentProfile, Contact
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import compat_webhooks as repo
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

from .test_compat_webhook_signature import retell_verify

_ALLOWED_HOST = "hooks.example.com"
_WEBHOOK_URL = f"https://{_ALLOWED_HOST}/retell"
_EVENTS = ["call_started", "call_ended", "call_analyzed"]


def _settings(**overrides: object):  # type: ignore[no-untyped-def]
    base = {"compat_webhook_allowed_hosts": _ALLOWED_HOST, "compat_webhook_delivery_enabled": True}
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def _fake_resolve(_host: str) -> list[str]:
    # example.com's documented address — globally routable, so resolve_public_or_raise passes
    # without real DNS (and never reaches the public network in tests). Async to match the
    # real ssrf_guard._resolve seam (which is awaited).
    return ["93.184.216.34"]


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def aiter_bytes(self):  # type: ignore[no-untyped-def]
        yield b"ok"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError("simulated receiver failure")


class _CaptureClient:
    """Stand-in for the httpx client: records every POST and returns a canned status."""

    def __init__(self, sink: list[dict[str, object]], status: int = 200) -> None:
        self.sink = sink
        self.status = status

    async def __aenter__(self) -> _CaptureClient:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def stream(self, method: str, url: str, *, content: bytes, headers: dict[str, str]) -> _Resp:
        self.sink.append(
            {"method": method, "url": url, "content": content, "headers": dict(headers)}
        )
        return _Resp(self.status)


def _factory(url: str):  # type: ignore[no-untyped-def]
    """Return (engine, sessionmaker) on a NullPool engine the caller disposes."""
    engine = create_async_engine(url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _org_id(factory) -> uuid.UUID:  # type: ignore[no-untyped-def]
    async with factory() as s:
        return (
            await s.execute(text("SELECT id FROM organizations WHERE slug='usan'"))
        ).scalar_one()


async def _seed_call_and_subscription(factory, org_id, settings, *, phone="+15550001111"):  # type: ignore[no-untyped-def]
    """Seed an agent profile + contact, register a webhook subscription, create a DIALING
    call bound to that agent. Returns (call_id, agent_profile_id, secret)."""
    async with factory() as s:
        await set_tenant_context(s, org_id)
        profile = AgentProfile(name="webhook-agent", draft_config={})
        contact = Contact(name="Test Caller", phone_e164=phone, timezone="America/New_York")
        s.add_all([profile, contact])
        await s.flush()
        _endpoint, secret = await repo.register_subscription(
            s,
            settings,
            agent_profile_id=profile.id,
            webhook_url=_WEBHOOK_URL,
            webhook_events=_EVENTS,
        )
        call = await calls_repo.create_call(
            s,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            profile_override=profile.id,
            livekit_room=f"usan-outbound-{uuid.uuid4().hex}",
        )
        await s.commit()
        return call.id, profile.id, secret


async def _run_full_lifecycle(url: str):  # type: ignore[no-untyped-def]
    settings = _settings()
    engine, factory = _factory(url)
    try:
        org_id = await _org_id(factory)
        call_id, _pid, secret = await _seed_call_and_subscription(factory, org_id, settings)

        # Native transitions fire the compat hooks: mark_answered -> call_started,
        # set_status(COMPLETED) -> call_ended. Each runs in its own org-scoped txn.
        async with factory() as s:
            await set_tenant_context(s, org_id)
            await calls_repo.mark_answered(s, call_id, sip_call_id="sip-1")
            await s.commit()
        async with factory() as s:
            await set_tenant_context(s, org_id)
            await calls_repo.set_status(s, call_id, CallStatus.COMPLETED)
            await s.commit()
        # call_analyzed shares the same helper that summarization calls.
        async with factory() as s:
            await set_tenant_context(s, org_id)
            from usan_api.compat.lifecycle import enqueue_compat_call_event

            call = await calls_repo.get_call(s, call_id)
            assert call is not None
            await enqueue_compat_call_event(s, call, event="call_analyzed")
            await s.commit()

        async with factory() as s:
            await set_tenant_context(s, org_id)
            enqueued = (
                await s.execute(
                    text(
                        "SELECT event, payload FROM compat_webhook_deliveries "
                        "ORDER BY created_at, id"
                    )
                )
            ).all()
            row_ids = {
                str(r[0])
                for r in (await s.execute(text("SELECT id FROM compat_webhook_deliveries"))).all()
            }

        stats = await cwd.poll_once(factory, settings, now=datetime.now(UTC))
        return {
            "secret": secret,
            "enqueued": [(e, p) for e, p in enqueued],
            "row_ids": row_ids,
            "stats": stats,
            "call_id": str(call_id),
        }
    finally:
        await engine.dispose()


def test_lifecycle_delivers_three_signed_events(
    async_database_url, compat_env, monkeypatch
) -> None:
    sink: list[dict[str, object]] = []
    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    monkeypatch.setattr(cwd, "_build_client", lambda settings: _CaptureClient(sink))

    out = asyncio.run(_run_full_lifecycle(async_database_url))

    # Three events enqueued in the {event, call_id} minimal shape.
    assert {e for e, _ in out["enqueued"]} == {"call_started", "call_ended", "call_analyzed"}
    for event, payload in out["enqueued"]:
        assert payload == {"event": event, "call_id": out["call_id"]}

    # Poller delivered all three.
    assert out["stats"]["delivered"] == 3
    assert len(sink) == 3

    delivered_events = set()
    delivered_ids = set()
    for req in sink:
        assert req["url"] == _WEBHOOK_URL
        headers = req["headers"]
        body = req["content"]
        # The body is the byte-faithful RetellAI {event, call} (delivery_id is a HEADER only).
        parsed = json.loads(body)
        assert set(parsed) == {"event", "call"}
        # The delivered body carries the RetellAI-shaped call_id (bare 32-hex), distinct from
        # the dashed str(uuid) kept in the internal enqueue payload.
        assert parsed["call"]["call_id"] == out["call_id"].replace("-", "")
        delivered_events.add(parsed["event"])
        # The CRM's unmodified retell-sdk verify() accepts our signature over the raw bytes.
        assert retell_verify(body, out["secret"], headers["x-retell-signature"]) is True
        delivered_ids.add(headers["x-retell-delivery-id"])

    assert delivered_events == {"call_started", "call_ended", "call_analyzed"}
    # Stable dedupe id: each header id is the (immutable) delivery row PK.
    assert delivered_ids == out["row_ids"]


async def _run_retry_stability(url: str):  # type: ignore[no-untyped-def]
    settings = _settings()
    engine, factory = _factory(url)
    try:
        org_id = await _org_id(factory)
        call_id, pid, secret = await _seed_call_and_subscription(
            factory, org_id, settings, phone="+15550002222"
        )
        async with factory() as s:
            await set_tenant_context(s, org_id)
            endpoint = await repo.get_subscription_for_agent(
                s, agent_profile_id=pid, event="call_ended"
            )
            assert endpoint is not None
            delivery = await repo.enqueue_call_event(
                s, endpoint_id=endpoint.id, event="call_ended", call_id=call_id
            )
            await s.commit()
            delivery_id = str(delivery.id)

        now = datetime.now(UTC)
        first_sink: list[dict[str, object]] = []
        # First attempt fails -> row stays pending, attempts bumped, retry scheduled.
        cwd._build_client = lambda settings: _CaptureClient(first_sink, status=500)  # type: ignore[assignment]
        s1 = await cwd.poll_once(factory, settings, now=now)

        second_sink: list[dict[str, object]] = []
        cwd._build_client = lambda settings: _CaptureClient(second_sink, status=200)  # type: ignore[assignment]
        # Advance past the 1-minute first rung so the row is due again.
        s2 = await cwd.poll_once(factory, settings, now=now + timedelta(minutes=2))
        return {
            "delivery_id": delivery_id,
            "first": (s1, [h["headers"]["x-retell-delivery-id"] for h in first_sink]),  # type: ignore[index]
            "second": (s2, [h["headers"]["x-retell-delivery-id"] for h in second_sink]),  # type: ignore[index]
            "secret": secret,
        }
    finally:
        await engine.dispose()


def test_delivery_id_stable_across_retry(async_database_url, compat_env, monkeypatch) -> None:
    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    out = asyncio.run(_run_retry_stability(async_database_url))

    s1, first_ids = out["first"]
    s2, second_ids = out["second"]
    assert s1["retry_scheduled"] == 1  # first attempt failed, not terminal
    assert s2["delivered"] == 1  # retry succeeded
    # The dedupe id is identical across the retry (it is the immutable row PK).
    assert first_ids == [out["delivery_id"]]
    assert second_ids == [out["delivery_id"]]


async def _try_register(url: str, settings, webhook_url: str) -> None:
    engine, factory = _factory(url)
    try:
        org_id = await _org_id(factory)
        async with factory() as s:
            await set_tenant_context(s, org_id)
            await repo.register_subscription(
                s,
                settings,
                agent_profile_id=uuid.uuid4(),
                webhook_url=webhook_url,
                webhook_events=["call_ended"],
            )
    finally:
        await engine.dispose()


def test_register_rejects_off_allow_list_host(async_database_url, compat_env) -> None:
    # Host not in COMPAT_WEBHOOK_ALLOWED_HOSTS -> 403 (no PHI webhook can be registered).
    with pytest.raises(CompatError) as exc:
        asyncio.run(_try_register(async_database_url, _settings(), "https://evil.example.org/x"))
    assert exc.value.status_code == 403


def test_register_rejects_when_allow_list_empty(async_database_url, compat_env) -> None:
    # Empty allow-list is fail-closed: every registration is rejected (no PHI ever leaves).
    settings = _settings(compat_webhook_allowed_hosts="")
    with pytest.raises(CompatError) as exc:
        asyncio.run(_try_register(async_database_url, settings, _WEBHOOK_URL))
    assert exc.value.status_code == 403


def test_register_rejects_non_https_url(async_database_url, compat_env) -> None:
    with pytest.raises(CompatError) as exc:
        asyncio.run(_try_register(async_database_url, _settings(), f"http://{_ALLOWED_HOST}/x"))
    assert exc.value.status_code == 422
