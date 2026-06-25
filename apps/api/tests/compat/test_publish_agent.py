"""T047 — POST /publish-agent/{agent_id} thin publish endpoint (feature 003, Phase 1b)."""

from __future__ import annotations

import pytest

from tests.compat.conftest import RETELL_VOICE

pytestmark = pytest.mark.frozen


def test_publish_agent_returns_published_agentresponse(compat_client, compat_headers):
    # create an agent WITHOUT publishing, then publish via the thin endpoint
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    agent = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
            "agent_name": "Pub Me",
        },
        headers=compat_headers,
    ).json()
    resp = compat_client.post(f"/publish-agent/{agent['agent_id']}", headers=compat_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_published"] is True
    assert body["version"] >= 1


def test_publish_agent_and_publish_agent_version_same_shape(compat_client, compat_headers):
    """Verify publish-agent and publish-agent-version produce the same response shape."""
    # Create two agents to publish separately
    for agent_name, endpoint, extra_json in [
        ("Pub Thin", "/publish-agent/{agent_id}", {}),
        ("Pub Versioned", "/publish-agent-version/{agent_id}", {"version": 0}),
    ]:
        llm = compat_client.post(
            "/create-retell-llm",
            json={"start_speaker": "agent", "general_prompt": "hi"},
            headers=compat_headers,
        ).json()
        agent = compat_client.post(
            "/create-agent",
            json={
                "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
                "voice_id": RETELL_VOICE,
                "agent_name": agent_name,
            },
            headers=compat_headers,
        ).json()
        if endpoint == "/publish-agent/{agent_id}":
            resp = compat_client.post(f"/publish-agent/{agent['agent_id']}", headers=compat_headers)
        else:
            resp = compat_client.post(
                f"/publish-agent-version/{agent['agent_id']}",
                json=extra_json,
                headers=compat_headers,
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Both must have the same top-level shape fields
        assert "agent_id" in body
        assert "is_published" in body
        assert "version" in body
        assert body["is_published"] is True
        assert body["version"] >= 1
