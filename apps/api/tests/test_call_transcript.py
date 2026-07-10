import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import transcripts as transcripts_repo

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).


async def _seed(async_database_url: str, room: str, *, with_tx: bool) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    base = datetime(2026, 6, 5, 1, 22, tzinfo=UTC)
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="A", phone_e164=phone, timezone="UTC"
            )
            call = await calls_repo.create_call(
                db,
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                livekit_room=room,
            )
            if with_tx:
                await transcripts_repo.create_transcript_segments(
                    db,
                    call_id=call.id,
                    segments=[
                        {
                            "role": "assistant",
                            "content": "Hello, daily check-in.",
                            "started_at": base,
                            "tool_name": None,
                            "tool_args": None,
                            "ended_at": None,
                        },
                        {
                            "role": "user",
                            "content": "I'm good, thank you.",
                            "started_at": base + timedelta(seconds=3),
                            "tool_name": None,
                            "tool_args": None,
                            "ended_at": None,
                        },
                        {
                            "role": "tool",
                            "content": "",
                            "started_at": base + timedelta(seconds=6),
                            "tool_name": "log_wellness",
                            "tool_args": {"mood": 5},
                            "ended_at": None,
                        },
                    ],
                )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_get_call_returns_transcript_segments_in_order(client, async_database_url):
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-tx1", with_tx=True))
    body = client.get(f"/v1/calls/{call_id}", headers=_OP).json()
    tx = body["transcript"]
    assert [s["role"] for s in tx] == ["assistant", "user", "tool"]
    assert tx[0]["content"] == "Hello, daily check-in."
    assert tx[2]["tool_name"] == "log_wellness"
    assert tx[2]["tool_args"] == {"mood": 5}


def test_get_call_without_transcript_returns_empty_list(client, async_database_url):
    # A call with no transcript yields an empty list, not null/missing.
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-tx0", with_tx=False))
    body = client.get(f"/v1/calls/{call_id}", headers=_OP).json()
    assert body["transcript"] == []


def test_get_call_with_transcript_emits_phi_access_audit_log(client, async_database_url):
    # Returning a transcript exposes PHI, so the access is audit-logged like the
    # recording path (spec §10): metadata only — segment count + real caller host —
    # and NEVER the transcript content itself.
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-txlog", with_tx=True))
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        client.get(f"/v1/calls/{call_id}", headers={**_OP, "X-Forwarded-For": "198.51.100.4"})
    finally:
        logger.remove(handler_id)

    audit = [r for r in records if r["message"] == "Transcript accessed"]
    assert len(audit) == 1
    assert audit[0]["extra"]["segments"] == 3
    assert audit[0]["extra"]["call_id"] == str(call_id)
    # The real client IP (X-Forwarded-For first hop behind Caddy), not the proxy.
    assert audit[0]["extra"]["client"] == "198.51.100.4"
    # PHI guard: transcript content must never appear in ANY log record — neither the
    # rendered message nor the bound `extra` fields (where a stray .bind() of PHI lands).
    rendered = [r["message"] + str(r["extra"]) for r in records]
    for phi in ("Hello, daily check-in.", "I'm good, thank you."):
        assert not any(phi in line for line in rendered)


def test_get_call_without_transcript_emits_no_phi_access_audit_log(client, async_database_url):
    # No transcript => no PHI exposed => no audit log (mirrors the recording path,
    # which only logs when a URL is actually issued).
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-txlog0", with_tx=False))
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        client.get(f"/v1/calls/{call_id}", headers=_OP)
    finally:
        logger.remove(handler_id)

    assert not any(r["message"] == "Transcript accessed" for r in records)


def test_list_for_call_caps_and_orders_segments(async_database_url, monkeypatch):
    # The query is bounded by MAX_TRANSCRIPT_SEGMENTS so a runaway transcript can't
    # produce an unbounded response. Patch the cap low and assert truncation keeps
    # the EARLIEST segments (ordered by started_at, id).
    monkeypatch.setattr(transcripts_repo, "MAX_TRANSCRIPT_SEGMENTS", 2)

    async def _run() -> list[str]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        phone = f"+1555{str(uuid.uuid4().int)[:7]}"
        base = datetime(2026, 6, 5, 1, 22, tzinfo=UTC)
        try:
            async with factory() as db:
                contact = await contacts_repo.create_contact(
                    db, name="A", phone_e164=phone, timezone="UTC"
                )
                call = await calls_repo.create_call(
                    db,
                    contact_id=contact.id,
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.COMPLETED,
                    livekit_room="usan-outbound-cap",
                )
                await transcripts_repo.create_transcript_segments(
                    db,
                    call_id=call.id,
                    segments=[
                        {
                            "role": "user",
                            "content": f"m{i}",
                            "started_at": base + timedelta(seconds=i),
                            "tool_name": None,
                            "tool_args": None,
                            "ended_at": None,
                        }
                        for i in range(3)
                    ],
                )
                await db.commit()
                rows = await transcripts_repo.list_for_call(db, call.id)
                return [r.content for r in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_run()) == ["m0", "m1"]
