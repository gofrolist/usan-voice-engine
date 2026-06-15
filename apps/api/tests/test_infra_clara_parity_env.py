"""Infra contract (T084/T086): the Clara Care Parity poller/inbound/Spanish keys are
plumbed through compose AND documented in both env examples.

Clone of test_infra_scheduler_env.py for spec 002. Without the api-service environment
plumbing, compose never passes these vars into the container — the pollers would silently
never run and the inbound webhook would 401 forever even with the VM .env set. SHIP-INERT:
every poller flag interpolates to ``false`` so a stale .env stays inert (compliance Gate 1).
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]

_ENABLE_KEYS = (
    "NOTIFICATION_OUTBOX_ENABLED",
    "CALLBACK_DIALER_POLLER_ENABLED",
    "FAMILY_REPORT_POLLER_ENABLED",
)
_INTERVAL_KEYS = (
    "NOTIFICATION_OUTBOX_POLL_INTERVAL_S",
    "CALLBACK_DIALER_POLL_INTERVAL_S",
    "FAMILY_REPORT_POLL_INTERVAL_S",
)
_VALUE_KEYS = ("TELNYX_INBOUND_PUBLIC_KEY", "SPANISH_PROFILE_ID")
_ALL_KEYS = _ENABLE_KEYS + _INTERVAL_KEYS + _VALUE_KEYS


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


def test_compose_api_service_has_all_parity_keys() -> None:
    env = _api_env("docker-compose.yml")
    for k in _ALL_KEYS:
        assert k in env, f"{k} missing from api service environment (container never gets it)"


def test_compose_poller_flags_ship_inert() -> None:
    env = _api_env("docker-compose.yml")
    # SHIP-INERT: every poller enable interpolates to false so a stale .env stays off.
    for k in _ENABLE_KEYS:
        assert env[k] == f"${{{k}:-false}}", f"{k} must default OFF, got {env[k]}"


def test_env_example_documents_parity_keys() -> None:
    text = (_REPO / "infra" / ".env.example").read_text()
    for k in _ENABLE_KEYS + _INTERVAL_KEYS:
        # Commented-defaults house style (the optional poller-tuning precedent).
        assert f"# {k}=" in text, f"{k} missing from .env.example as a commented default"
    for k in _VALUE_KEYS:
        # Operator-supplied secrets/ids are live-blank, like TELNYX_FROM_NUMBER.
        assert f"{k}=" in text, f"{k} missing from .env.example"


def test_env_prod_example_documents_parity_keys() -> None:
    text = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in _ALL_KEYS:
        assert k in text, f"{k} missing from .env.prod.example"
    # Ship inert in prod: the enable flags are live-value OFF.
    for k in _ENABLE_KEYS:
        assert f"{k}=false" in text, f"{k} must be pinned OFF in .env.prod.example"
