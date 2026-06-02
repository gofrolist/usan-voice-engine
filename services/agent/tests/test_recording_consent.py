from unittest.mock import AsyncMock

from usan_agent.pipeline import GREETING, RECORDING_DISCLOSURE, greet


def test_recording_disclosure_mentions_recording():
    assert "record" in RECORDING_DISCLOSURE.lower()
    assert RECORDING_DISCLOSURE.strip()


async def test_greet_speaks_disclosure_then_greeting():
    session = AsyncMock()
    await greet(session)
    spoken = [call.args[0] for call in session.say.await_args_list]
    assert spoken == [RECORDING_DISCLOSURE, GREETING]
    # The disclosure is non-interruptible so it always plays in full.
    assert session.say.await_args_list[0].kwargs.get("allow_interruptions") is False
