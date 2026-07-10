"""Phase 7 slice 2: PUT /rerun-call-analysis/{call_id} — 201 V2CallResponse, best-effort.

404 only for missing/archived; unconfigured / transcript-less / Vertex-failure all keep
the prior analysis and still answer 201 (mirrors rerun-chat-analysis). The recompute
upserts conversation_summaries, which get-call then reflects.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import _published_agent_id, create_call
from usan_api import summarization
from usan_api.compat import ids
from usan_api.vertex_test import VertexTurn


def _vertex_returns(monkeypatch, summary: str) -> None:
    monkeypatch.setattr(
        summarization,
        "run_vertex_turn",
        AsyncMock(
            return_value=VertexTurn(
                text=json.dumps({"summary": summary, "open_plans": [], "facts": []})
            )
        ),
    )


async def _add_transcript(async_database_url: str, call_id: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await db.execute(
                text(
                    "INSERT INTO transcripts (call_id, role, content, started_at) "
                    "VALUES (CAST(:c AS uuid), 'user', 'I feel great today', now())"
                ),
                {"c": str(ids.decode_call_id(call_id))},
            )
            await db.commit()
    finally:
        await engine.dispose()


def _seed_call(compat_client, compat_headers) -> str:
    agent_id = _published_agent_id(compat_client, compat_headers)
    r = create_call(compat_client, compat_headers, override_agent_id=agent_id)
    assert r.status_code == 201, r.text
    return r.json()["call_id"]


def test_rerun_unknown_call_404(compat_client, compat_headers):
    r = compat_client.put(
        f"/rerun-call-analysis/{ids.encode_call_id(uuid.uuid4())}", headers=compat_headers
    )
    assert r.status_code == 404


@pytest.mark.frozen
async def test_rerun_populates_analysis_and_conforms(
    compat_client,
    compat_headers,
    mock_dispatch,
    allow_quiet_hours,
    summarization_on,
    monkeypatch,
    async_database_url,
):
    call_id = _seed_call(compat_client, compat_headers)
    await _add_transcript(async_database_url, call_id)

    _vertex_returns(monkeypatch, "A cheerful check-in.")
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["call_analysis"]["call_summary"] == "A cheerful check-in."
    assert_conforms(body, "V2PhoneCallResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneCallResponse")

    # get-call reflects the persisted analysis.
    g = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers).json()
    assert g["call_analysis"]["call_summary"] == "A cheerful check-in."

    # A second rerun REPLACES it (upsert, not insert-once).
    _vertex_returns(monkeypatch, "Actually a somber check-in.")
    r2 = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r2.status_code == 201
    assert r2.json()["call_analysis"]["call_summary"] == "Actually a somber check-in."


def test_rerun_unconfigured_still_201(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    """No summarization_on override: flag off -> no Vertex call, prior (absent) analysis."""
    call_id = _seed_call(compat_client, compat_headers)
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()  # non-terminal call, no summary -> omitted


def test_rerun_without_transcript_still_201(
    compat_client,
    compat_headers,
    mock_dispatch,
    allow_quiet_hours,
    summarization_on,
    monkeypatch,
):
    call_id = _seed_call(compat_client, compat_headers)
    _vertex_returns(monkeypatch, "never used")
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()


async def test_rerun_vertex_failure_keeps_prior_and_201(
    compat_client,
    compat_headers,
    mock_dispatch,
    allow_quiet_hours,
    summarization_on,
    monkeypatch,
    async_database_url,
):
    call_id = _seed_call(compat_client, compat_headers)
    await _add_transcript(async_database_url, call_id)
    monkeypatch.setattr(
        summarization, "run_vertex_turn", AsyncMock(side_effect=RuntimeError("boom"))
    )
    r = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    assert "call_analysis" not in r.json()

    # And the endpoint still works on a later request (session not poisoned).
    g = compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers)
    assert g.status_code == 200


@pytest.mark.frozen
async def test_rerun_web_call_conforms(
    compat_client,
    compat_headers,
    web_agent_id,
    mock_web_dispatch,
    summarization_on,
    monkeypatch,
    async_database_url,
):
    """Contact-less web-call rerun: recompute persists (contact_id NULL) and the 201 body
    conforms to the WEB branch of V2CallResponse."""
    r = compat_client.post(
        "/v2/create-web-call", json={"agent_id": web_agent_id}, headers=compat_headers
    )
    assert r.status_code == 201, r.text
    call_id = r.json()["call_id"]
    await _add_transcript(async_database_url, call_id)

    _vertex_returns(monkeypatch, "A web chat recap.")
    rr = compat_client.put(f"/rerun-call-analysis/{call_id}", headers=compat_headers)
    assert rr.status_code == 201, rr.text
    body = rr.json()
    assert body["call_analysis"]["call_summary"] == "A web chat recap."
    assert_conforms(body, "V2WebCallResponse")
    assert_sdk_roundtrip(body, "retell.types:WebCallResponse")
