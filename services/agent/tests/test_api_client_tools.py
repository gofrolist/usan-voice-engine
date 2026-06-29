import httpx
import pytest

from usan_agent import api_client
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="g",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="s" * 32,
    )


class _FakeClient:
    """Stands in for httpx.AsyncClient; records the request and returns a canned response."""

    captured: dict = {}
    status = 200
    json_data: dict = {}

    def __init__(self, timeout=None):
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        request = httpx.Request("POST", url)
        return httpx.Response(_FakeClient.status, json=_FakeClient.json_data, request=request)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.status = 200
    _FakeClient.json_data = {}
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def test_log_wellness_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 7}
    await api_client.log_wellness("call-1", _settings(), mood=4, pain_level=2, notes="ok")
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/log_wellness"
    assert cap["json"] == {"call_id": "call-1", "mood": 4, "pain_level": 2, "notes": "ok"}
    assert cap["headers"]["Authorization"].startswith("Bearer ")


async def test_log_medication_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 9}
    await api_client.log_medication(
        "call-1", _settings(), medication_name="Aspirin", taken=True, reported_time=None
    )
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/log_medication"
    assert cap["json"] == {
        "call_id": "call-1",
        "medication_name": "Aspirin",
        "taken": True,
        "reported_time": None,
    }


async def test_get_today_meds_returns_medications(fake_http):
    med_entry = {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}
    _FakeClient.json_data = {"medications": [med_entry]}
    meds = await api_client.get_today_meds("call-1", _settings())
    assert meds == [med_entry]
    assert fake_http.captured["json"] == {"call_id": "call-1"}


async def test_report_end_call_posts_reason(fake_http):
    _FakeClient.json_data = {"status": "completed"}
    await api_client.report_end_call("call-1", _settings(), "check_in_complete")
    assert fake_http.captured["url"] == "http://api:8000/v1/tools/end_call"
    assert fake_http.captured["json"] == {"call_id": "call-1", "reason": "check_in_complete"}


async def test_tool_client_raises_on_http_error(fake_http):
    _FakeClient.status = 500
    with pytest.raises(httpx.HTTPStatusError):
        await api_client.log_wellness("call-1", _settings(), mood=3, pain_level=None, notes=None)


async def test_schedule_callback_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 11}
    await api_client.schedule_callback(
        "call-1",
        _settings(),
        requested_time_text="tomorrow afternoon",
        requested_at="2026-06-10T15:00:00Z",
        notes="prefers afternoons",
    )
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/schedule_callback"
    assert cap["json"] == {
        "call_id": "call-1",
        "requested_time_text": "tomorrow afternoon",
        "requested_at": "2026-06-10T15:00:00Z",
        "notes": "prefers afternoons",
    }
    assert cap["headers"]["Authorization"].startswith("Bearer ")


async def test_schedule_callback_passes_null_optionals(fake_http):
    _FakeClient.json_data = {"id": 12}
    await api_client.schedule_callback(
        "call-1", _settings(), requested_time_text="soon", requested_at=None, notes=None
    )
    assert fake_http.captured["json"] == {
        "call_id": "call-1",
        "requested_time_text": "soon",
        "requested_at": None,
        "notes": None,
    }


async def test_record_personal_fact_posts_scoped_request(fake_http):
    # US4: the agent->API boundary for the memory-write tool. structured defaults to {}
    # when omitted (so an important_date with no date still posts a well-formed body).
    _FakeClient.json_data = {"id": 789}
    await api_client.record_personal_fact(
        "call-1", _settings(), category="person", content="daughter Maria visits Sundays"
    )
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/record_personal_fact"
    assert cap["json"] == {
        "call_id": "call-1",
        "category": "person",
        "content": "daughter Maria visits Sundays",
        "structured": {},
    }
    assert cap["headers"]["Authorization"].startswith("Bearer ")


async def test_record_personal_fact_forwards_structured(fake_http):
    # A provided structured payload (e.g. an important_date's date) is forwarded verbatim,
    # so build_memory_params can window it into the important_dates builtin next call.
    _FakeClient.json_data = {"id": 790}
    await api_client.record_personal_fact(
        "call-1",
        _settings(),
        category="important_date",
        content="her birthday",
        structured={"date": "2026-07-04"},
    )
    assert fake_http.captured["json"]["structured"] == {"date": "2026-07-04"}


async def test_retrieve_kb_context_posts_scoped_request_and_returns_context(fake_http):
    _FakeClient.json_data = {"context": "kb says hello", "hit_count": 2}
    out = await api_client.retrieve_kb_context("call-1", _settings(), "how are meds taken")
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/retrieve_kb_context"
    assert cap["json"] == {"call_id": "call-1", "query": "how are meds taken"}
    assert cap["headers"]["Authorization"].startswith("Bearer ")
    assert out == "kb says hello"


async def test_retrieve_kb_context_returns_empty_on_http_error(fake_http):
    _FakeClient.status = 500  # raise_for_status() will raise httpx.HTTPStatusError
    out = await api_client.retrieve_kb_context("call-1", _settings(), "q")
    assert out == ""


async def test_retrieve_kb_context_returns_empty_on_missing_context_field(fake_http):
    _FakeClient.json_data = {"hit_count": 0}
    out = await api_client.retrieve_kb_context("call-1", _settings(), "q")
    assert out == ""
