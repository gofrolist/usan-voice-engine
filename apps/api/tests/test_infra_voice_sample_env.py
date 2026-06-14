"""Regression guard: the API container must receive CARTESIA_API_KEY.

The voice-sample preview endpoint (GET /v1/admin/voice-catalog/{id}/sample) runs in
the API service and calls Cartesia TTS. If the api service in docker-compose.yml does
not pass CARTESIA_API_KEY, the endpoint 503s ("CARTESIA_API_KEY unset") even when the
key is present in the secret / .env — because only the *agent* service was wired with
it. This pins the api service + env-example docs so that regression can't recur.
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]


def _api_env_keys() -> set[str]:
    doc = yaml.safe_load((_REPO / "infra" / "docker-compose.yml").read_text())
    env = doc["services"]["api"]["environment"]
    # environment may be a dict or a list of "K: v" / "K=v"; normalize to a key set.
    if isinstance(env, dict):
        return set(env.keys())
    return {str(item).split("=")[0].split(":")[0].strip() for item in env}


def test_compose_api_service_has_cartesia_api_key() -> None:
    assert "CARTESIA_API_KEY" in _api_env_keys(), (
        "api service must pass CARTESIA_API_KEY (the voice-sample endpoint runs in the "
        "API; without it, Play sample 503s — the agent service alone is not enough)"
    )


def test_env_examples_document_cartesia_api_key() -> None:
    for rel in ("infra/.env.example", "infra/.env.prod.example"):
        text = (_REPO / rel).read_text()
        assert "CARTESIA_API_KEY" in text, f"CARTESIA_API_KEY missing from {rel}"
