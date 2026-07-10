import asyncio
import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from usan_api import object_storage, phi_audit
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.settings import get_settings

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).


async def _seed(async_database_url: str, room: str, *, recording_uri=None) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
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
            if recording_uri is not None:
                await calls_repo.set_recording_uri(db, room, recording_uri)
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_get_call_without_recording_has_no_presigned_url(client, async_database_url):
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-norec"))
    body = client.get(f"/v1/calls/{call_id}", headers=_OP).json()
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
        lambda gs_uri, ttl, *, expected_bucket=None: f"https://signed.example/{gs_uri}",
    )
    body = client.get(f"/v1/calls/{call_id}", headers=_OP).json()
    assert body["recording_uri"] == uri
    assert body["presigned_recording_url"] == f"https://signed.example/{uri}"


def test_get_call_signing_error_returns_200_with_null_url(client, async_database_url, monkeypatch):
    # A signing failure (e.g. GCS/IAM unavailable) must degrade gracefully: the call
    # is still returned (200) with a null presigned URL, never a 500.
    uri = "gs://test-bucket/recordings/2026-06-02/x.ogg"
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-signerr", recording_uri=uri))
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()

    def _boom(gs_uri, ttl, *, expected_bucket=None):
        raise RuntimeError("signBlob unavailable")

    monkeypatch.setattr(object_storage, "generate_signed_url", _boom)
    response = client.get(f"/v1/calls/{call_id}", headers=_OP)
    assert response.status_code == 200
    body = response.json()
    assert body["recording_uri"] == uri
    assert body["presigned_recording_url"] is None


def test_operator_get_call_records_never_bind_actor(client, async_database_url, monkeypatch):
    # Behavioral-refactor guard for the recording_urls extraction: the operator plane
    # passes no actor, so the locked-sink record stays bit-identical — ids/host/flag
    # only, never an actor key (and never the URL itself).
    uri = "gs://test-bucket/recordings/2026-06-02/x.ogg"
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-noactor", recording_uri=uri))
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()
    monkeypatch.setattr(
        object_storage,
        "generate_signed_url",
        lambda gs_uri, ttl, *, expected_bucket=None: f"https://signed.example/{gs_uri}",
    )
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        response = client.get(f"/v1/calls/{call_id}", headers=_OP)
        assert response.status_code == 200
    finally:
        logger.remove(handler_id)

    accessed = [r for r in records if r["message"] == phi_audit.RECORDING_URL_ACCESSED]
    assert len(accessed) == 1
    extra = accessed[0]["extra"]
    assert extra["call_id"] == str(call_id)
    assert extra["client"]
    assert extra["has_recording"] is True
    assert "actor" not in extra
