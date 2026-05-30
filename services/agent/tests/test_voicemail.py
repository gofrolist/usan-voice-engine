import asyncio

import pytest

from usan_agent.voicemail import VOICEMAIL_WINDOW_S, VoicemailWatcher, is_voicemail


@pytest.mark.parametrize(
    "text",
    [
        "Please leave a message after the tone",
        "leave a name and number",
        "You've reached the Smith residence",  # straight apostrophe
        "you’ve reached us",  # curly apostrophe (U+2019)
        "youve reached us",  # dropped apostrophe
        "I'm not available right now",
        "please record your message after the beep",
    ],
)
def test_is_voicemail_true(text):
    assert is_voicemail(text) is True


@pytest.mark.parametrize(
    "text",
    ["Hello?", "Hi, this is Ada speaking", "who is this", ""],
)
def test_is_voicemail_false(text):
    assert is_voicemail(text) is False


def test_watcher_accumulates_across_chunks():
    w = VoicemailWatcher()
    w.feed("please")
    assert w.detected is False
    w.feed("leave a message")  # buffer now matches across chunks
    assert w.detected is True


@pytest.mark.asyncio
async def test_watcher_wait_detects_within_window():
    w = VoicemailWatcher()

    async def _later():
        await asyncio.sleep(0.01)
        w.feed("you've reached the Smiths")

    asyncio.ensure_future(_later())
    assert await w.wait_until_detected(window_s=VOICEMAIL_WINDOW_S) is True


@pytest.mark.asyncio
async def test_watcher_wait_times_out_for_human():
    w = VoicemailWatcher()
    w.feed("hello who is this")
    assert await w.wait_until_detected(window_s=0.05) is False
