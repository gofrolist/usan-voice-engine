import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import transcripts as transcripts_repo

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).
_OP = {"Authorization": "Bearer " + "o" * 32}


async def _seed(async_database_url: str, room: str, *, with_tx: bool) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    base = datetime(2026, 6, 5, 1, 22, tzinfo=UTC)
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db,
                elder_id=elder.id,
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
