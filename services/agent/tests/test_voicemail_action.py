from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import voicemail_action
from usan_agent.pipeline import VOICEMAIL_MESSAGE


def _settings() -> MagicMock:
    return MagicMock()


def _awaitable_handle() -> AsyncMock:
    """Return an AsyncMock that can be used as a directly-awaitable SpeechHandle.

    session.say() returns a SpeechHandle which implements __await__. We simulate
    this with an AsyncMock used as a coroutine function — the production code does
    `handle = session.say(...); await handle`, so session.say is AsyncMock and
    handle is the coroutine object returned by calling it.
    """
    return AsyncMock()


@pytest.mark.asyncio
async def test_leave_voicemail_sequence(monkeypatch):
    reported = []

    async def _fake_report(call_id, settings):
        reported.append(call_id)

    monkeypatch.setattr(voicemail_action, "report_voicemail_left", _fake_report)

    ctx = MagicMock()
    ctx.delete_room = AsyncMock()
    ctx.shutdown = MagicMock()

    # session.say is AsyncMock: calling it returns a coroutine (awaitable handle).
    session = MagicMock()
    session.interrupt = MagicMock()
    session.say = AsyncMock()

    await voicemail_action.leave_voicemail(ctx, session, "call-789", _settings())

    session.interrupt.assert_called_once_with(force=True)
    session.say.assert_awaited_once()
    assert session.say.call_args.args[0] == VOICEMAIL_MESSAGE
    assert session.say.call_args.kwargs["allow_interruptions"] is False
    assert reported == ["call-789"]
    ctx.delete_room.assert_awaited_once()
    ctx.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_leave_voicemail_skips_report_without_call_id(monkeypatch):
    called = []
    monkeypatch.setattr(
        voicemail_action,
        "report_voicemail_left",
        AsyncMock(side_effect=lambda *a: called.append(a)),
    )
    ctx = MagicMock()
    ctx.delete_room = AsyncMock()
    ctx.shutdown = MagicMock()
    session = MagicMock()
    session.interrupt = MagicMock()
    session.say = AsyncMock()

    await voicemail_action.leave_voicemail(ctx, session, None, _settings())

    assert called == []  # no call_id → nothing to report
    ctx.delete_room.assert_awaited_once()
