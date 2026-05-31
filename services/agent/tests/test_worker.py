from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import worker
from usan_agent.voicemail import VoicemailWatcher
from usan_agent.worker import CallMetadata, parse_metadata


def test_parse_metadata_outbound():
    raw = '{"call_id": "abc", "direction": "outbound", "dynamic_vars": {"name": "Ada"}}'
    md = parse_metadata(raw)
    assert md == CallMetadata(call_id="abc", direction="outbound", dynamic_vars={"name": "Ada"})


def test_parse_metadata_none_is_inbound():
    md = parse_metadata(None)
    assert md.call_id is None
    assert md.direction == "inbound"
    assert md.dynamic_vars == {}


def test_parse_metadata_empty_string_is_inbound():
    md = parse_metadata("")
    assert md.direction == "inbound"
    assert md.call_id is None


def test_parse_metadata_invalid_json_is_inbound():
    md = parse_metadata("not json")
    assert md.direction == "inbound"
    assert md.call_id is None
    assert md.dynamic_vars == {}


@pytest.mark.asyncio
async def test_run_detection_window_triggers_voicemail(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)

    watcher = VoicemailWatcher()
    watcher.feed("you've reached the Smiths, leave a message")  # already detected

    session = MagicMock()
    ctx = MagicMock()
    greeted = []

    async def _greet(_s):
        greeted.append(True)

    monkeypatch.setattr(worker, "greet", _greet)

    await worker._run_detection_window(ctx, session, watcher, call_id="c1", settings=MagicMock())

    assert greeted == [True]
    assert left == ["c1"]


@pytest.mark.asyncio
async def test_run_detection_window_human_falls_through(monkeypatch):
    left = []

    async def _fake_leave(ctx, session, call_id, settings):
        left.append(call_id)

    monkeypatch.setattr(worker, "leave_voicemail", _fake_leave)
    monkeypatch.setattr(worker, "greet", AsyncMock())
    # shorten the window so the test is fast
    monkeypatch.setattr(worker, "VOICEMAIL_WINDOW_S", 0.05)

    watcher = VoicemailWatcher()  # never fed a voicemail phrase

    await worker._run_detection_window(
        MagicMock(), MagicMock(), watcher, call_id="c2", settings=MagicMock()
    )

    assert left == []  # human → no voicemail action
