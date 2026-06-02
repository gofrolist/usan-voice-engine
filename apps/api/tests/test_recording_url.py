import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import object_storage
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import get_settings


async def _seed(async_database_url: str, room: str, *, recording_uri=None) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
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
            if recording_uri is not None:
                await calls_repo.set_recording_uri(db, room, recording_uri)
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_get_call_without_recording_has_no_presigned_url(client, async_database_url):
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-norec"))
    body = client.get(f"/v1/calls/{call_id}").json()
    assert body["recording_uri"] is None
    assert body["presigned_recording_url"] is None
    assert body["egress_id"] is None


def test_get_call_with_recording_returns_signed_url(client, async_database_url, monkeypatch):
    uri = "gs://test-bucket/recordings/2026-06-02/x.ogg"
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-sign1", recording_uri=uri))
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()
    monkeypatch.setattr(
        object_storage,
        "generate_signed_url",
        lambda gs_uri, ttl: f"https://signed.example/{gs_uri}",
    )
    body = client.get(f"/v1/calls/{call_id}").json()
    assert body["recording_uri"] == uri
    assert body["presigned_recording_url"] == f"https://signed.example/{uri}"


def test_get_call_signing_error_returns_200_with_null_url(client, async_database_url, monkeypatch):
    # A signing failure (e.g. GCS/IAM unavailable) must degrade gracefully: the call
    # is still returned (200) with a null presigned URL, never a 500.
    uri = "gs://test-bucket/recordings/2026-06-02/x.ogg"
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-signerr", recording_uri=uri))
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()

    def _boom(gs_uri, ttl):
        raise RuntimeError("signBlob unavailable")

    monkeypatch.setattr(object_storage, "generate_signed_url", _boom)
    response = client.get(f"/v1/calls/{call_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["recording_uri"] == uri
    assert body["presigned_recording_url"] is None
