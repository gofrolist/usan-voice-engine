from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from prometheus_client import REGISTRY

from usan_api.main import create_app


def _counter(name: str, labels: dict[str, str]) -> float:
    """Current value of a labeled counter sample (0.0 if not yet observed)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _set_min_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum env for create_app() to build (no DB connection needed)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/u")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_metrics_endpoint_exposes_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "usan_calls_total" in body
    assert "usan_webhooks_total" in body
    assert "usan_tool_calls_total" in body
    assert "http_request" in body


def test_create_app_twice_does_not_raise_duplicate_timeseries(monkeypatch):
    _set_min_env(monkeypatch)
    app1 = create_app()
    app2 = create_app()  # must NOT raise "Duplicated timeseries in CollectorRegistry"
    assert any(getattr(r, "path", None) == "/metrics" for r in app1.routes)
    assert any(getattr(r, "path", None) == "/metrics" for r in app2.routes)


def test_metrics_endpoint_is_not_rate_limited(client):
    for _ in range(5):
        assert client.get("/metrics").status_code == 200


def _service_jwt(call_id: UUID, signing_key: str = "s" * 32) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": "usan-agent",
            "call_id": str(call_id),
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        signing_key,
        algorithm="HS256",
    )


@pytest.mark.xfail(strict=True, reason="router increment wired in Task 3")
def test_tool_call_error_path_increments_counter(client):
    cid = uuid4()
    labels = {"tool": "log_metrics", "outcome": "error"}
    before = _counter("usan_tool_calls_total", labels)
    r = client.post(
        "/v1/tools/log_metrics",
        json={
            "call_id": str(cid),
            "turns": [],
            "usage": {
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "tts_characters": 0,
                "stt_audio_seconds": 0.0,
                "session_duration_seconds": 0.0,
            },
        },
        headers={"Authorization": f"Bearer {_service_jwt(cid)}"},
    )
    assert r.status_code == 404
    assert _counter("usan_tool_calls_total", labels) == before + 1


@pytest.mark.xfail(strict=True, reason="router increment wired in Task 3")
def test_invalid_webhook_increments_counter(client):
    labels = {"type": "unknown", "outcome": "invalid"}
    before = _counter("usan_webhooks_total", labels)
    r = client.post("/webhooks/livekit", content=b"{}", headers={"Authorization": "bad"})
    assert r.status_code == 401
    assert _counter("usan_webhooks_total", labels) == before + 1
