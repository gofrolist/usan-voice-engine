"""T012 / T034 (US1+US2): crisis escalation integration.

A raise_crisis call creates an urgent follow_up_flags row with the crisis columns
populated and, for each registered family contact opted in to crisis alerts, enqueues a
PHI-minimized family alert (call_id IS NULL) and sets family_notified. When no family
contact exists, the urgent flag surfaces the absence to operators (FR-013) and
family_notified stays False. Both detection paths firing for the same (call_id, category)
merge detection_source to 'both' and never double-text.
"""

import asyncio
import json
import time
import uuid

import jwt
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.models import FollowUpFlag, SmsMessage

_OP = {"Authorization": "Bearer " + "o" * 32}
_FAMILY = "+15557654321"


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _create_contact(client) -> str:
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _add_contact(
    url: str, contact_id: str, *, phone: str = _FAMILY, prefs: dict | None = None
) -> None:
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO family_contacts (contact_id, name, phone_e164, alert_prefs) "
                        "VALUES (:e, 'Dana', :p, CAST(:prefs AS JSONB))"
                    ),
                    {"e": contact_id, "p": phone, "prefs": json.dumps(prefs or {})},
                )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_do())


def _enqueue(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"esc-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def _load_flag(url: str, flag_id: int) -> FollowUpFlag:
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                return await db.get(FollowUpFlag, flag_id)
        finally:
            await engine.dispose()

    return asyncio.run(_do())


def _notifications_for_flag(url: str, flag_id: int) -> list[SmsMessage]:
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                rows = (
                    (await db.execute(select(SmsMessage).where(SmsMessage.call_id.is_(None))))
                    .scalars()
                    .all()
                )
                return [
                    r
                    for r in rows
                    if r.dedupe_key and r.dedupe_key.startswith(f"crisis:{flag_id}:")
                ]
        finally:
            await engine.dispose()

    return asyncio.run(_do())


def test_crisis_creates_urgent_flag_with_crisis_columns(client, mock_dispatch, async_database_url):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "safety_net"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    flag = _load_flag(async_database_url, r.json()["flag_id"])
    assert flag.severity == "urgent"
    assert flag.category == "safety"
    assert flag.status == "open"
    assert flag.crisis_category == "medical"
    assert flag.detection_source == "safety_net"
    assert flag.resource_offered == "medical"


def test_crisis_enqueues_phi_minimized_family_alert(client, mock_dispatch, async_database_url):
    contact_id = _create_contact(client)
    _add_contact(async_database_url, contact_id, phone=_FAMILY)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "suicidal", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    flag_id = r.json()["flag_id"]
    alerts = _notifications_for_flag(async_database_url, flag_id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == "family_alert"
    assert alert.call_id is None
    assert alert.to_number == _FAMILY
    assert alert.status == "pending"
    # PHI-minimized: no clinical content in the body.
    low = alert.body.lower()
    for term in ("mood", "pain", "medication", "suicid", "overdose"):
        assert term not in low
    flag = _load_flag(async_database_url, flag_id)
    assert flag.family_notified is True


def test_crisis_respects_contact_alert_prefs_opt_out(client, mock_dispatch, async_database_url):
    # An explicit crisis=false opt-out excludes the contact; with no opted-in contact the
    # flag itself is the operator-queue signal (FR-013).
    contact_id = _create_contact(client)
    _add_contact(async_database_url, contact_id, phone=_FAMILY, prefs={"crisis": False})
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    flag_id = r.json()["flag_id"]
    assert _notifications_for_flag(async_database_url, flag_id) == []
    flag = _load_flag(async_database_url, flag_id)
    assert flag.family_notified is False


def test_crisis_without_family_contact_surfaces_to_operator(
    client, mock_dispatch, async_database_url
):
    contact_id = _create_contact(client)  # no family contact registered
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "abuse", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    flag_id = r.json()["flag_id"]
    assert _notifications_for_flag(async_database_url, flag_id) == []
    flag = _load_flag(async_database_url, flag_id)
    assert flag.family_notified is False
    # FR-013: the absence is surfaced to operators on the urgent flag.
    assert flag.reason is not None
    assert "family" in flag.reason.lower()


def test_crisis_both_detection_sources_merge_to_both(client, mock_dispatch, async_database_url):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "overdose", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    r2 = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "overdose", "detection_source": "safety_net"},
        headers=_auth(call_id),
    )
    assert r2.status_code == 200, r2.text
    flag = _load_flag(async_database_url, r2.json()["flag_id"])
    assert flag.detection_source == "both"


def test_crisis_family_alert_deduped_across_both_paths(client, mock_dispatch, async_database_url):
    # Both detection paths fire for the same call+category -> one flag, one family alert.
    contact_id = _create_contact(client)
    _add_contact(async_database_url, contact_id, phone=_FAMILY)
    call_id = _enqueue(client, contact_id)
    a = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "safety_net"},
        headers=_auth(call_id),
    )
    flag_id = a.json()["flag_id"]
    assert len(_notifications_for_flag(async_database_url, flag_id)) == 1
