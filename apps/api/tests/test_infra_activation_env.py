"""Infra contract: the v0.12.0 (RetellAI-parity) feature flags reach their containers.

These flags gate real runtime behavior (chat analysis, KB retrieval / RAG, inbound SMS,
voice-RAG on the worker). A flag that is NOT plumbed through the compose ``environment:``
map is stuck at its False default no matter what the VM ``.env`` says — the exact gap that
left this batch un-activatable after deploy. SHIP-INERT: every *_ENABLED flag must
interpolate to ``false`` so a stale/blank .env keeps the feature off.

This is the parity guard that was missing when the batch shipped; extend ``_API_FLAGS`` /
``_AGENT_FLAGS`` whenever a new runtime flag is added.
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]

# api-process flags newly wired for activation (Phase 4b-2/4b-3 SMS, 4c-2 chat analysis,
# 5b/5c KB retrieval). *_ENABLED entries are additionally asserted ship-inert below.
_API_FLAGS = (
    "CHAT_ANALYSIS_ENABLED",
    "CHAT_ANALYSIS_MODEL",
    "TELNYX_INBOUND_SMS_REPLY_ENABLED",
    "TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED",
    "KB_RETRIEVAL_ENABLED",
    "KB_RETRIEVAL_VOICE_ENABLED",
    "KB_RETRIEVAL_TOP_K",
    "KB_RETRIEVAL_MAX_DISTANCE",
    "KB_RETRIEVAL_MAX_CONTEXT_CHARS",
)
# worker (agent) flags — voice-RAG gate + per-turn timeout, plus the flow-runtime gate.
_AGENT_FLAGS = (
    "KB_RETRIEVAL_VOICE_ENABLED",
    "KB_RETRIEVAL_TIMEOUT_S",
    "FLOW_RUNTIME_VOICE_ENABLED",
)
_INERT_ENABLED = tuple(k for k in (*_API_FLAGS, *_AGENT_FLAGS) if k.endswith("_ENABLED"))


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader tolerating compose overlay tags (!reset / !override in prod overlay)."""


def _construct_unknown(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_unknown)


def _service_env(service: str) -> dict[str, str]:
    doc = yaml.load((_REPO / "infra" / "docker-compose.yml").read_text(), Loader=_ComposeLoader)
    env = doc["services"][service]["environment"]
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    pairs = (str(item).split("=", 1) for item in env)
    return {p[0].strip(): p[1].strip() if len(p) > 1 else "" for p in pairs}


def test_api_service_wires_activation_flags() -> None:
    env = _service_env("api")
    for k in _API_FLAGS:
        assert k in env, f"{k} missing from api service environment (container never gets it)"


def test_agent_service_wires_voice_flags() -> None:
    env = _service_env("agent")
    for k in _AGENT_FLAGS:
        assert k in env, f"{k} missing from agent service environment (worker never gets it)"


def test_activation_flags_ship_inert() -> None:
    api, agent = _service_env("api"), _service_env("agent")
    for k in _INERT_ENABLED:
        val = api.get(k, agent.get(k, ""))
        assert val == f"${{{k}:-false}}", f"{k} must interpolate to false (ship-inert), got {val!r}"


def test_env_prod_example_documents_activation_flags() -> None:
    prod = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in (*_API_FLAGS, *_AGENT_FLAGS, "KB_RETRIEVAL_TIMEOUT_S"):
        assert k in prod, f"{k} missing from .env.prod.example"
