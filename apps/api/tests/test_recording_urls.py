"""Unit tests for ``usan_api.recording_urls`` — the single presigned-URL signing path.

Pure unit tests (no DB): a fake ``Call`` via ``types.SimpleNamespace`` and a
monkeypatched ``object_storage.generate_signed_url`` that captures its arguments and
returns a sentinel URL. Pins the spec §9 rows: TTL unclamped on the operator plane,
``min(settings TTL, max_ttl_s)`` on the admin plane, ``expected_bucket`` forwarded,
signing offloaded via ``asyncio.to_thread``, the None/WARN fallbacks, and the
locked-sink success line that never contains the (bearer-secret) URL.
"""

import asyncio
import types
import uuid

from loguru import logger

from usan_api import object_storage, phi_audit, recording_urls
from usan_api.settings import Settings

SENTINEL_URL = "https://storage.example/SIGNED-SENTINEL"


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
        "GCS_BUCKET": "test-bucket",
        "RECORDING_SIGNED_URL_TTL_S": "3600",
    }
    base.update(overrides)
    return Settings(**base)


def _call(recording_uri="gs://test-bucket/recordings/2026-06-10/x.ogg") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), recording_uri=recording_uri)


def _capture_signer(monkeypatch) -> list[tuple]:
    """Monkeypatch the signer to capture (gs_uri, ttl, expected_bucket) calls."""
    captured: list[tuple] = []

    def fake_sign(gs_uri, ttl, *, expected_bucket=None):
        captured.append((gs_uri, ttl, expected_bucket))
        return SENTINEL_URL

    monkeypatch.setattr(object_storage, "generate_signed_url", fake_sign)
    return captured


def test_ttl_unclamped_without_max(monkeypatch):
    # Operator plane bit-identical: no max_ttl_s -> the settings TTL goes through as-is.
    captured = _capture_signer(monkeypatch)
    call = _call()

    url = asyncio.run(
        recording_urls.presigned_recording_url(call, _settings(), client_host="10.0.0.9")
    )

    assert url == SENTINEL_URL
    assert len(captured) == 1
    assert captured[0][1] == 3600


def test_ttl_clamped_with_max(monkeypatch):
    # Admin-plane ceiling: min(settings TTL, max_ttl_s) — a true min, never a raise.
    captured = _capture_signer(monkeypatch)

    url = asyncio.run(
        recording_urls.presigned_recording_url(
            _call(),
            _settings(),
            client_host="10.0.0.9",
            max_ttl_s=recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S,
        )
    )
    assert url == SENTINEL_URL
    assert captured[-1][1] == 600

    # A settings TTL below the ceiling wins (min, not override).
    asyncio.run(
        recording_urls.presigned_recording_url(
            _call(),
            _settings(RECORDING_SIGNED_URL_TTL_S="300"),
            client_host="10.0.0.9",
            max_ttl_s=600,
        )
    )
    assert captured[-1][1] == 300


def test_expected_bucket_passed(monkeypatch):
    # Fail-closed bucket confinement: settings.gcs_bucket is forwarded to the signer.
    captured = _capture_signer(monkeypatch)
    settings = _settings()
    call = _call()

    asyncio.run(recording_urls.presigned_recording_url(call, settings, client_host="10.0.0.9"))

    assert captured == [(call.recording_uri, 3600, settings.gcs_bucket)]


def test_signing_called_via_thread(monkeypatch):
    # Pins the §9 "called via thread" row non-vacuously: a plain signer-mock cannot
    # distinguish the asyncio.to_thread offload from a direct (loop-blocking) call.
    _capture_signer(monkeypatch)
    thread_calls: list = []

    async def fake_to_thread(func, /, *args, **kwargs):
        thread_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    url = asyncio.run(
        recording_urls.presigned_recording_url(_call(), _settings(), client_host="10.0.0.9")
    )

    assert url == SENTINEL_URL
    assert thread_calls == [object_storage.generate_signed_url]


def test_none_paths(monkeypatch):
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        # No recording_uri -> None, no log line.
        captured = _capture_signer(monkeypatch)
        result = asyncio.run(
            recording_urls.presigned_recording_url(
                _call(recording_uri=None), _settings(), client_host="10.0.0.9"
            )
        )
        assert result is None
        assert records == []

        # No bucket configured -> None, no log line.
        result = asyncio.run(
            recording_urls.presigned_recording_url(
                _call(), _settings(GCS_BUCKET=None), client_host="10.0.0.9"
            )
        )
        assert result is None
        assert records == []
        assert captured == []

        # Signing failure -> None + the existing WARNING copy (never a raise).
        def _boom(gs_uri, ttl, *, expected_bucket=None):
            raise RuntimeError("signBlob unavailable")

        monkeypatch.setattr(object_storage, "generate_signed_url", _boom)
        result = asyncio.run(
            recording_urls.presigned_recording_url(_call(), _settings(), client_host="10.0.0.9")
        )
        assert result is None
        warnings = [r for r in records if r["level"].name == "WARNING"]
        assert len(warnings) == 1
        assert warnings[0]["message"] == "Failed to sign recording URL"
    finally:
        logger.remove(handler_id)


def test_success_emits_locked_sink_line_actor_optional(monkeypatch):
    _capture_signer(monkeypatch)
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        call = _call()
        url = asyncio.run(
            recording_urls.presigned_recording_url(call, _settings(), client_host="10.0.0.9")
        )
        assert url == SENTINEL_URL
        assert len(records) == 1
        record = records[0]
        assert record["message"] == phi_audit.RECORDING_URL_ACCESSED
        assert record["extra"]["call_id"] == str(call.id)
        assert record["extra"]["client"] == "10.0.0.9"
        assert record["extra"]["has_recording"] is True
        # Operator-plane bit-identical contract: no actor key unless one was given.
        assert "actor" not in record["extra"]

        asyncio.run(
            recording_urls.presigned_recording_url(
                call, _settings(), client_host="10.0.0.9", actor="nurse@usan.org"
            )
        )
        assert len(records) == 2
        assert records[1]["extra"]["actor"] == "nurse@usan.org"

        # URLs are bearer secrets: the signed sentinel must never reach any log record
        # (non-vacuous — the mocked signer returned a real URL above).
        for rec in records:
            assert "SIGNED-SENTINEL" not in rec["message"]
            assert "SIGNED-SENTINEL" not in repr(rec["extra"])
    finally:
        logger.remove(handler_id)
