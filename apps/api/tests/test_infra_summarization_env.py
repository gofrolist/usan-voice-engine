"""Infra contract: the Vertex + summarization keys reach the api container (review H3).

US4 post-call summarization and the US8 family-report narrative gate on
``summarization_enabled and gcp_project`` (summarization.py). Without SUMMARIZATION_ENABLED
plumbed through the api service environment, compose never passes it in, so it is stuck at
its False default in prod and the narrative silently falls back to the deterministic
template. SHIP-INERT: the flag interpolates to ``false`` so a stale .env stays inert.
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_KEYS = ("GCP_PROJECT", "VERTEX_LOCATION", "SUMMARIZATION_ENABLED", "SUMMARIZATION_MODEL")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader tolerating compose overlay tags (!reset / !override in prod overlay)."""


def _construct_unknown(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_unknown)


def _api_env(compose_file: str) -> dict[str, str]:
    doc = yaml.load((_REPO / "infra" / compose_file).read_text(), Loader=_ComposeLoader)
    env = doc["services"]["api"]["environment"]
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    pairs = (str(item).split("=", 1) for item in env)
    return {p[0].strip(): p[1].strip() if len(p) > 1 else "" for p in pairs}


def test_compose_api_service_has_summarization_keys() -> None:
    env = _api_env("docker-compose.yml")
    for k in _KEYS:
        assert k in env, f"{k} missing from api service environment (container never gets it)"


def test_summarization_flag_ships_inert() -> None:
    env = _api_env("docker-compose.yml")
    assert env["SUMMARIZATION_ENABLED"] == "${SUMMARIZATION_ENABLED:-false}"


def test_env_examples_document_summarization_keys() -> None:
    ex = (_REPO / "infra" / ".env.example").read_text()
    prod = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in ("SUMMARIZATION_ENABLED", "SUMMARIZATION_MODEL"):
        assert k in ex, f"{k} missing from .env.example"
        assert k in prod, f"{k} missing from .env.prod.example"
