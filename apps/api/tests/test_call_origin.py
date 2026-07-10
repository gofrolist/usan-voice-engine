"""C5: derived `origin` provenance on `CallResponse` (spec §4.3).

`GET /v1/calls/{id}` parses the call's own `idempotency_key` against the
materializer's reserved namespace (`sched:`/`batch:`). Operator keys, retry
children (no key by design — chain walk via `parent_call_id` is the documented
provenance), and malformed stored values all degrade to `None` — never raise.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.schemas.call import CallOrigin, parse_origin


def test_parse_origin_schedule_key() -> None:
    u = str(uuid.uuid4())
    origin = parse_origin(f"sched:{u}:2026-06-10")
    assert origin == CallOrigin(source="schedule", id=uuid.UUID(u), ordinal="2026-06-10")


def test_parse_origin_batch_key() -> None:
    u = str(uuid.uuid4())
    origin = parse_origin(f"batch:{u}:7")
    assert origin is not None
    assert origin.source == "batch"
    assert origin.id == uuid.UUID(u)
    assert origin.ordinal == 7
    assert isinstance(origin.ordinal, int)


def test_parse_origin_none_for_operator_keys_and_garbage() -> None:
    u = str(uuid.uuid4())
    assert parse_origin(None) is None
    assert parse_origin("daily-2026") is None
    assert parse_origin("sched:notauuid:x") is None
    assert parse_origin(f"batch:{u}") is None  # missing ordinal


@pytest.fixture
def mock_dispatch(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from usan_api import dialer, livekit_dispatch

    agent = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)
    monkeypatch.setattr(dialer, "schedule_dial", lambda *a, **k: None)
    return agent


def _create_contact(client: TestClient) -> str:
    r = client.post(
        "/v1/contacts",
        json={"name": "Ada", "phone_e164": "+15551234567", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 201
    return str(r.json()["id"])


def _seed_batch_chain(async_database_url: str, contact_id: str, batch_key: str) -> tuple[str, str]:
    """Insert a materializer-style root (`batch:` key) and a keyless retry child.

    Direct repo/model writes on a local NullPool engine: `CreateCallRequest`
    rejects reserved prefixes, so these rows can only exist via the materializer.
    """
    from usan_api.repositories import calls as calls_repo

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                root = await calls_repo.create_call(
                    db,
                    contact_id=uuid.UUID(contact_id),
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                    idempotency_key=batch_key,
                )
                child = Call(
                    contact_id=uuid.UUID(contact_id),
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                    parent_call_id=root.id,
                    attempt=2,
                )
                db.add(child)
                await db.commit()
                return str(root.id), str(child.id)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_get_call_response_includes_origin(
    client: TestClient, mock_dispatch: AsyncMock, async_database_url: str
) -> None:
    contact_id = _create_contact(client)
    batch_id = str(uuid.uuid4())
    root_id, child_id = _seed_batch_chain(async_database_url, contact_id, f"batch:{batch_id}:0")

    # Materialized batch root: origin is parsed from its own key.
    r = client.get(f"/v1/calls/{root_id}", headers=_OP)
    assert r.status_code == 200
    assert r.json()["origin"] == {"source": "batch", "id": batch_id, "ordinal": 0}

    # Operator enqueue: a non-reserved key carries no provenance.
    created = client.post(
        "/v1/calls",
        json={"contact_id": contact_id, "idempotency_key": "op-key-1", "dynamic_vars": {}},
        headers=_OP,
    )
    assert created.status_code == 202
    r = client.get(f"/v1/calls/{created.json()['id']}", headers=_OP)
    assert r.status_code == 200
    assert r.json()["origin"] is None

    # Retry child: no key by design -> None (chain walk via parent_call_id, §4.3).
    r = client.get(f"/v1/calls/{child_id}", headers=_OP)
    assert r.status_code == 200
    assert r.json()["origin"] is None
