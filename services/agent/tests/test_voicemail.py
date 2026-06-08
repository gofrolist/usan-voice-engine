import asyncio

import pytest

from usan_agent.voicemail import (
    VOICEMAIL_WINDOW_S,
    VoicemailWatcher,
    build_matcher,
    is_voicemail,
)


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


def test_build_matcher_empty_uses_builtin():
    from usan_agent.voicemail import _PATTERN

    assert build_matcher([]) is _PATTERN


def test_build_matcher_custom_phrases_literal_and_case_insensitive():
    matcher = build_matcher(["please record your message"])
    assert matcher.search("PLEASE RECORD YOUR MESSAGE now")
    assert not matcher.search("you've reached the Smiths")  # built-in phrase NOT included


def test_build_matcher_escapes_regex_metachars():
    # A phrase with regex metachars must match literally, not as a pattern.
    matcher = build_matcher(["press 1 (now)"])
    assert matcher.search("please press 1 (now) to continue")


def test_build_matcher_blank_phrases_fall_back_to_builtin():
    from usan_agent.voicemail import _PATTERN

    assert build_matcher(["   ", ""]) is _PATTERN


def test_watcher_uses_injected_matcher():
    matcher = build_matcher(["custom greeting marker"])
    w = VoicemailWatcher(matcher=matcher)
    w.feed("this is a custom greeting marker hello")
    assert w.detected


def test_watcher_default_matcher_unchanged():
    w = VoicemailWatcher()
    w.feed("you've reached the Smiths, leave a message")
    assert w.detected
