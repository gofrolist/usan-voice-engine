from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_KEYS = (
    "TELNYX_MESSAGING_API_KEY",
    "TELNYX_MESSAGING_PROFILE_ID",
    "TELNYX_FROM_NUMBER",
    "TELNYX_MESSAGING_ENABLED",
)


def test_compose_api_service_has_messaging_env():
    doc = yaml.safe_load((_REPO / "infra" / "docker-compose.yml").read_text())
    env = doc["services"]["api"]["environment"]
    # environment may be a dict or list of "K: v" / "K=v"; normalize to a key set.
    if isinstance(env, dict):
        keys = set(env.keys())
    else:
        keys = {str(item).split("=")[0].split(":")[0].strip() for item in env}
    for k in _KEYS:
        assert k in keys, f"{k} missing from api service environment"


def test_env_example_contains_messaging_keys():
    text = (_REPO / "infra" / ".env.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.example"


def test_env_prod_example_contains_messaging_keys():
    text = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.prod.example"
