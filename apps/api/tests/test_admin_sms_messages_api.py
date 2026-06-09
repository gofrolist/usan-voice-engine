import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


async def _seed(url: str, *, status: str) -> uuid.UUID:
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db,
                elder_id=elder.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
            )
            row = await sms_repo.create_sms_message(
                db,
                call_id=call.id,
                elder_id=elder.id,
                to_number=phone,
                template_key="t",
                body="SECRET-BODY-TEXT",
            )
            if status != "pending":
                await sms_repo.mark_failed(db, row.id, error={"reason": "x"})
            await db.commit()
            return row.id
    finally:
        await engine.dispose()


def test_sms_messages_requires_admin_session(client):
    r = client.get("/v1/admin/sms-messages")
    assert r.status_code == 401


def test_sms_messages_lists_and_omits_body(client, admin_session, async_database_url):
    sms_id = asyncio.run(_seed(async_database_url, status="pending"))
    r = client.get("/v1/admin/sms-messages")
    assert r.status_code == 200
    items = r.json()
    assert any(i["id"] == str(sms_id) for i in items)
    for i in items:
        assert "body" not in i  # SmsMessageSummary OMITS the rendered body
        assert set(i.keys()) >= {"id", "call_id", "elder_id", "to_number", "template_key", "status"}


def test_sms_messages_status_filter(client, admin_session, async_database_url):
    asyncio.run(_seed(async_database_url, status="failed"))
    r = client.get("/v1/admin/sms-messages?status=failed")
    assert r.status_code == 200
    assert len(r.json()) >= 1
    assert all(i["status"] == "failed" for i in r.json())
