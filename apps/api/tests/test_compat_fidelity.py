"""T041 — supporting lookups + compatibility-fidelity tests (feature 003, US5).

Covers the read-only catalog endpoints (``list-voices`` / ``get-voice`` /
``get-concurrency``), the out-of-scope ``501 not_supported`` stubs, the RetellAI
``{status,message}`` error-envelope fidelity, and id consistency (SC-006: the same
resource presents the same compat id across create / get / list). Driven over the
real HTTP path against the mounted compat sub-app.
"""

from __future__ import annotations

import pytest

from usan_api.schemas.voice_catalog import VOICE_CATALOG

_SPEC = VOICE_CATALOG[0]  # the live-call default (Sarah - Mindful Woman)
_CARTESIA = _SPEC.cartesia_voice_id
_RETELL = "retell-" + _SPEC.name.split(" - ")[0].split()[0]


def _published_agent_id(client, headers) -> str:
    llm = client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=headers,
    ).json()
    agent = client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": _RETELL,
            "agent_name": "Fidelity Bot",
        },
        headers=headers,
    ).json()
    return agent["agent_id"]


# --- auth (app-level gate runs before every route, incl. read-only + stubs) -------------
def test_list_voices_requires_key(compat_client):
    assert compat_client.get("/list-voices").status_code == 401


# --- list-voices / get-voice -----------------------------------------------------------
def test_list_voices_bare_array_shape(compat_client, compat_headers):
    r = compat_client.get("/list-voices", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)  # bare array, not a wrapper object
    assert body  # non-empty
    sample = body[0]
    for key in ("voice_id", "voice_name", "provider", "accent", "gender", "age"):
        assert key in sample
    assert all(v["provider"] == "cartesia" for v in body)
    assert _RETELL in {v["voice_id"] for v in body}  # the default voice's alias is listed


def test_get_voice_by_retell_alias(compat_client, compat_headers):
    r = compat_client.get(f"/get-voice/{_RETELL}", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["voice_id"] == _RETELL
    assert body["voice_name"] == _SPEC.name
    assert body["provider"] == "cartesia"


def test_get_voice_by_raw_cartesia_id(compat_client, compat_headers):
    # A raw curated cartesia id is hosted as-is and resolves to the same canonical voice.
    r = compat_client.get(f"/get-voice/{_CARTESIA}", headers=compat_headers)
    assert r.status_code == 200
    assert r.json()["voice_name"] == _SPEC.name


def test_get_voice_unknown_returns_404_envelope(compat_client, compat_headers):
    r = compat_client.get("/get-voice/retell-NotAHostedVoice", headers=compat_headers)
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert "message" in body
    assert "detail" not in body


# --- get-concurrency -------------------------------------------------------------------
def test_get_concurrency_shape_and_values(compat_client, compat_headers):
    r = compat_client.get("/get-concurrency", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "current_concurrency",
        "concurrency_limit",
        "base_concurrency",
        "purchased_concurrency",
        "concurrency_purchase_limit",
        "remaining_purchase_limit",
        "reserved_inbound_concurrency",
        "concurrency_burst_enabled",
        "concurrency_burst_limit",
    ):
        assert key in body
    assert body["current_concurrency"] == 0  # no in-flight calls in this test
    assert body["concurrency_limit"] >= 1
    assert body["base_concurrency"] == body["concurrency_limit"]
    assert body["reserved_inbound_concurrency"] < body["concurrency_limit"]
    assert body["purchased_concurrency"] == 0
    assert body["concurrency_purchase_limit"] == 0
    assert body["remaining_purchase_limit"] == 0
    assert body["concurrency_burst_enabled"] is False


# --- out-of-scope → 501 not_supported --------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/create-conversation-flow"),
        ("post", "/create-knowledge-base"),
        ("post", "/create-chat"),
        ("post", "/v2/create-web-call"),
        ("post", "/clone-voice"),
        ("get", "/get-mcp-tools"),
        ("get", "/list-export-requests"),
        ("post", "/agent-playground-completion"),
        ("post", "/create-phone-number"),
    ],
)
def test_out_of_scope_returns_501_envelope(compat_client, compat_headers, method, path):
    # The stubs read no body; GET via TestClient rejects a json= kwarg, so omit it.
    r = getattr(compat_client, method)(path, headers=compat_headers)
    assert r.status_code == 501
    body = r.json()
    assert body["status"] == 501
    assert body["message"].startswith("not_supported")
    assert "detail" not in body  # RetellAI envelope, not native {detail}


def test_out_of_scope_with_path_param_returns_501(compat_client, compat_headers):
    r = compat_client.get("/get-phone-number/pn_123", headers=compat_headers)
    assert r.status_code == 501
    assert r.json()["message"].startswith("not_supported")


def test_unsupported_still_requires_key(compat_client):
    # The app-level auth gate runs before the stub: no key → 401, not 501.
    assert compat_client.post("/create-conversation-flow", json={}).status_code == 401


# --- id consistency (SC-006) -----------------------------------------------------------
def test_agent_id_consistent_across_create_get_list(compat_client, compat_headers):
    agent_id = _published_agent_id(compat_client, compat_headers)
    got = compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).json()
    assert got["agent_id"] == agent_id
    listed = compat_client.get("/list-agents", headers=compat_headers).json()
    assert agent_id in {a["agent_id"] for a in listed}
