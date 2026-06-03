import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import recording
from usan_agent.settings import Settings

_BASE_ENV = {
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "CARTESIA_API_KEY": "c",
    "GEMINI_API_KEY": "g",
    "DEFAULT_CARTESIA_VOICE_ID": "v",
    "API_BASE_URL": "http://api:8000",
    "JWT_SIGNING_KEY": "s" * 32,
}


@pytest.fixture
def settings_with_bucket(monkeypatch):
    for k, v in {**_BASE_ENV, "GCS_BUCKET": "usan-rec"}.items():
        monkeypatch.setenv(k, v)
    return Settings()


@pytest.fixture
def settings_no_bucket(monkeypatch):
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    return Settings()


def test_recording_filepath_format():
    fixed = datetime.datetime(2026, 6, 2, 15, 0, tzinfo=datetime.UTC)
    out = recording.recording_filepath("abc-123", now=fixed, token="deadbeef")
    assert out == "recordings/2026-06-02/abc-123-deadbeef.ogg"


def test_http_url_converts_ws_scheme():
    assert recording._http_url("ws://livekit:7880") == "http://livekit:7880"
    assert recording._http_url("wss://lk.example") == "https://lk.example"


async def test_recording_disabled_without_bucket(monkeypatch, settings_no_bucket):
    made = MagicMock()
    monkeypatch.setattr(recording.api, "LiveKitAPI", made)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    result = await recording.start_call_recording(ctx, "call-1", settings_no_bucket)
    assert result is None
    made.assert_not_called()


async def test_start_call_recording_builds_audio_only_ogg_request(
    monkeypatch, settings_with_bucket
):
    egress = MagicMock()
    egress.start_room_composite_egress = AsyncMock(return_value=SimpleNamespace(egress_id="EG_7"))
    lkapi = MagicMock()
    lkapi.egress = egress
    lkapi.__aenter__ = AsyncMock(return_value=lkapi)
    lkapi.__aexit__ = AsyncMock(return_value=False)
    captured: dict = {}

    def _factory(url, api_key, api_secret):
        captured["url"] = url
        return lkapi

    monkeypatch.setattr(recording.api, "LiveKitAPI", _factory)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-9"))

    result = await recording.start_call_recording(ctx, "call-9", settings_with_bucket)

    assert result == "EG_7"
    assert captured["url"] == "http://livekit:7880"
    req = egress.start_room_composite_egress.await_args.args[0]
    assert req.room_name == "room-9"
    assert req.audio_only is True
    assert len(req.file_outputs) == 1
    out = req.file_outputs[0]
    assert out.file_type == recording.api.EncodedFileType.OGG
    assert out.filepath.startswith("recordings/")
    # unique per-attempt token suffix keeps the key non-overwriting (objectCreator-safe)
    assert "/call-9-" in out.filepath
    assert out.filepath.endswith(".ogg")
    assert out.gcp.bucket == "usan-rec"
    assert out.gcp.credentials == ""


async def test_start_call_recording_best_effort_on_error(monkeypatch, settings_with_bucket):
    egress = MagicMock()
    egress.start_room_composite_egress = AsyncMock(side_effect=RuntimeError("boom"))
    lkapi = MagicMock()
    lkapi.egress = egress
    lkapi.__aenter__ = AsyncMock(return_value=lkapi)
    lkapi.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(recording.api, "LiveKitAPI", lambda **kw: lkapi)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-x"))
    result = await recording.start_call_recording(ctx, "call-x", settings_with_bucket)
    assert result is None
