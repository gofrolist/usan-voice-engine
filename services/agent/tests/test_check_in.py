from unittest.mock import AsyncMock, MagicMock

from usan_agent import check_in
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c",
        GEMINI_API_KEY="g",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="s" * 32,
    )


def _data(job_ctx=None) -> check_in.CheckInData:
    ctx = job_ctx or MagicMock()
    return check_in.CheckInData(call_id="call-1", settings=_settings(), job_ctx=ctx)


async def test_do_log_wellness_calls_api_and_acks(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "log_wellness", spy)
    result = await check_in._do_log_wellness(_data(), mood=4, pain_level=2, notes="ok")
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs == {"mood": 4, "pain_level": 2, "notes": "ok"}
    assert spy.await_args.args[0] == "call-1"
    assert isinstance(result, str)
    assert result  # a spoken acknowledgement


async def test_do_log_wellness_handles_api_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "log_wellness", _boom)
    result = await check_in._do_log_wellness(_data(), mood=3, pain_level=None, notes=None)
    assert isinstance(result, str)
    assert result  # graceful string, no exception


async def test_do_log_medication_calls_api(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "log_medication", spy)
    result = await check_in._do_log_medication(_data(), medication_name="Aspirin", taken=True)
    spy.assert_awaited_once()
    assert spy.await_args.kwargs == {
        "medication_name": "Aspirin",
        "taken": True,
        "reported_time": None,
    }
    assert isinstance(result, str)
    assert result


async def test_do_get_today_meds_formats_list(monkeypatch):
    async def _meds(call_id, settings):
        return [
            {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]},
            {"name": "Metformin", "dosage": None, "times": ["08:00", "20:00"]},
        ]

    monkeypatch.setattr(check_in.api_client, "get_today_meds", _meds)
    result = await check_in._do_get_today_meds(_data())
    assert "Aspirin" in result
    assert "Metformin" in result


async def test_do_get_today_meds_empty(monkeypatch):
    async def _meds(call_id, settings):
        return []

    monkeypatch.setattr(check_in.api_client, "get_today_meds", _meds)
    result = await check_in._do_get_today_meds(_data())
    assert isinstance(result, str)
    assert result  # a "no medications" style message


async def test_do_end_call_reports_says_goodbye_and_hangs_up(monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "report_end_call", report)

    job_ctx = MagicMock()
    job_ctx.delete_room = AsyncMock()
    job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()

    manager = MagicMock()
    manager.attach_mock(session.say, "say")
    manager.attach_mock(job_ctx.delete_room, "delete_room")
    manager.attach_mock(job_ctx.shutdown, "shutdown")

    await check_in._do_end_call(_data(job_ctx=job_ctx), session, "check_in_complete")

    report.assert_awaited_once()
    session.say.assert_awaited_once()  # goodbye
    job_ctx.delete_room.assert_awaited_once()  # hang up
    job_ctx.shutdown.assert_called_once()

    # Goodbye must complete before delete_room, which must precede shutdown.
    names = [c[0] for c in manager.mock_calls]
    assert names.index("say") < names.index("delete_room") < names.index("shutdown")


async def test_do_end_call_hangs_up_even_if_report_fails(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "report_end_call", _boom)
    job_ctx = MagicMock()
    job_ctx.delete_room = AsyncMock()
    job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()

    await check_in._do_end_call(_data(job_ctx=job_ctx), session, "x")

    job_ctx.delete_room.assert_awaited_once()  # report failure must not block hangup
    job_ctx.shutdown.assert_called_once()


def test_build_check_in_agent_attaches_four_tools():
    agent = check_in.build_check_in_agent()
    # livekit-agents 1.5.14: FunctionTool has no .name; use .id (== function name)
    names = {t.id for t in agent.tools}
    assert names == {"log_wellness", "log_medication", "get_today_meds", "end_call"}
    assert agent.instructions == check_in.CHECK_IN_INSTRUCTIONS


def test_inbound_instructions_includes_name():
    text = check_in._inbound_instructions({"elder_name": "Ada"})
    assert "Ada" in text
    assert "last check-in" not in text  # no history line when absent


def test_inbound_instructions_includes_last_check_in():
    text = check_in._inbound_instructions(
        {"elder_name": "Ada", "last_check_in": "on 2026-05-30, mood 4/5"}
    )
    assert "Ada" in text
    assert "on 2026-05-30, mood 4/5" in text


def test_inbound_instructions_defaults_when_unknown():
    text = check_in._inbound_instructions({})
    assert "the caller" in text


def test_build_inbound_agent_has_same_four_tools():
    agent = check_in.build_inbound_agent({"elder_name": "Ada"})
    names = {t.id for t in agent.tools}
    assert names == {"log_wellness", "log_medication", "get_today_meds", "end_call"}
    assert "Ada" in agent.instructions
