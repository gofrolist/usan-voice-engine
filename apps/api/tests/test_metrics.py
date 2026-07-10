import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import service_token as _service_token
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallMetrics, TurnMetrics
from usan_api.repositories.metrics import response_latency_ms


def test_response_latency_sums_present_components():
    assert response_latency_ms(120, 210, 80) == 410


def test_response_latency_ignores_none():
    assert response_latency_ms(None, 210, 80) == 290


def test_response_latency_all_none_is_none():
    assert response_latency_ms(None, None, None) is None


def _make_completed_call(async_database_url: str, duration_seconds: int | None = 120) -> str:
    async def _run() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            call = Call(
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                duration_seconds=duration_seconds,
            )
            s.add(call)
            await s.flush()
            cid = str(call.id)
            await s.commit()
        await engine.dispose()
        return cid

    return asyncio.run(_run())


def _count(async_database_url: str, model, call_id: str) -> int:
    async def _run() -> int:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(
                select(func.count()).select_from(model).where(model.call_id == call_id)
            )
            n = result.scalar_one()
        await engine.dispose()
        return n

    return asyncio.run(_run())


_BODY = {
    "turns": [
        {
            "turn_index": 0,
            "eou_delay_ms": 180,
            "transcription_delay_ms": 120,
            "stt_duration_ms": 90,
            "llm_ttft_ms": 210,
            "tts_ttfb_ms": 80,
            "llm_completion_tokens": 50,
            "tts_characters": 240,
        }
    ],
    "usage": {
        "llm_prompt_tokens": 100,
        "llm_completion_tokens": 50,
        "tts_characters": 240,
        "stt_audio_seconds": 3.0,
        "session_duration_seconds": 42.0,
    },
}


def test_log_metrics_requires_token(client, async_database_url):
    cid = _make_completed_call(async_database_url)
    r = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY})
    assert r.status_code == 401


def test_log_metrics_call_id_mismatch_403(client, async_database_url):
    cid = _make_completed_call(async_database_url)
    other = "00000000-0000-0000-0000-000000000999"
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(other)}"},
    )
    assert r.status_code == 403


def test_log_metrics_unknown_call_404(client):
    missing = "00000000-0000-0000-0000-0000000000aa"
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": missing, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(missing)}"},
    )
    assert r.status_code == 404


def test_log_metrics_persists_and_costs_telephony(client, async_database_url):
    cid = _make_completed_call(async_database_url, duration_seconds=120)
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 200
    # default Telnyx rate 0.008, others 0 → 120s = 2 min × 0.008 = 0.016
    assert float(r.json()["cost_total_usd"]) == 0.016
    assert _count(async_database_url, CallMetrics, cid) == 1
    assert _count(async_database_url, TurnMetrics, cid) == 1


def test_log_metrics_is_idempotent(client, async_database_url):
    cid = _make_completed_call(async_database_url, duration_seconds=120)
    headers = {"Authorization": f"Bearer {_service_token(cid)}"}
    r1 = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY}, headers=headers)
    r2 = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY}, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _count(async_database_url, CallMetrics, cid) == 1
    assert _count(async_database_url, TurnMetrics, cid) == 1


def test_log_metrics_rejects_negative_usage(client, async_database_url):
    cid = _make_completed_call(async_database_url)
    bad = {"turns": [], "usage": {"stt_audio_seconds": -1.0}}
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **bad},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 422


def test_log_metrics_falls_back_to_session_duration(client, async_database_url):
    cid = _make_completed_call(async_database_url, duration_seconds=None)
    body = {
        "turns": [],
        "usage": {
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "tts_characters": 0,
            "stt_audio_seconds": 0.0,
            "session_duration_seconds": 120.0,
        },
    }
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **body},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 200
    # fallback duration 120s = 2 min × default 0.008 = 0.016 telephony
    assert float(r.json()["cost_total_usd"]) == 0.016
