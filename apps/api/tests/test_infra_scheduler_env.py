"""Infra contract: the 8 scheduler/gate keys are plumbed through compose + env examples.

Clone of test_infra_messaging_env.py for batch & scheduled calling (spec
2026-06-10 §5.1/§9/§10): dev compose defaults both feature flags ON for local
testing; the prod overlay re-pins them OFF so a stale VM .env interpolates to
the safe/inert state.
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_KEYS = (
    "SCHEDULER_POLLER_ENABLED",
    "SCHEDULER_POLL_INTERVAL_S",
    "SCHEDULER_BATCH_SIZE",
    "CONCURRENCY_GATE_ENABLED",
    "MAX_CONCURRENT_CALLS",
    "RESERVED_CONCURRENCY",
    "MAX_AUTONOMOUS_CALLS_PER_CONTACT_PER_DAY",
    "AUTONOMOUS_DIALING_PAUSED",
)


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
    # SafeLoader subclass: safe_load-equivalent, plus plain-data handling of the
    # compose overlay tags above (never constructs arbitrary Python objects).
    doc = yaml.load((_REPO / "infra" / compose_file).read_text(), Loader=_ComposeLoader)
    env = doc["services"]["api"]["environment"]
    # environment may be a dict or a list of "K=v" strings; normalize to key -> value.
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    pairs = (str(item).split("=", 1) for item in env)
    return {p[0].strip(): p[1].strip() if len(p) > 1 else "" for p in pairs}


def test_compose_api_service_has_scheduler_env() -> None:
    env = _api_env("docker-compose.yml")
    for k in _KEYS:
        assert k in env, f"{k} missing from api service environment"


def test_dev_compose_enables_both_flags() -> None:
    env = _api_env("docker-compose.yml")
    # Dev compose defaults both feature flags ON (spec §5.1/§9 dev-compose pin).
    assert env["SCHEDULER_POLLER_ENABLED"] == "${SCHEDULER_POLLER_ENABLED:-true}"
    assert env["CONCURRENCY_GATE_ENABLED"] == "${CONCURRENCY_GATE_ENABLED:-true}"


def test_prod_overlay_pins_flags_off() -> None:
    env = _api_env("docker-compose.prod.yml")
    # The prod overlay's environment map overrides the dev-on base file, so a
    # stale /opt/usan/infra/.env interpolates the defaults => safe/off (spec §10.2).
    assert env["SCHEDULER_POLLER_ENABLED"] == "${SCHEDULER_POLLER_ENABLED:-false}"
    assert env["CONCURRENCY_GATE_ENABLED"] == "${CONCURRENCY_GATE_ENABLED:-false}"


def test_env_example_contains_scheduler_keys() -> None:
    text = (_REPO / "infra" / ".env.example").read_text()
    for k in _KEYS:
        # Commented-defaults house style (retry-orchestrator block precedent).
        assert f"# {k}=" in text, f"{k} missing from .env.example as a commented default"


def test_env_prod_example_contains_scheduler_keys() -> None:
    text = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.prod.example"
    # Ship inert (spec §10.1): both feature flags are live-value OFF in prod.
    assert "SCHEDULER_POLLER_ENABLED=false" in text
    assert "CONCURRENCY_GATE_ENABLED=false" in text


def test_alert_rule_file_provisioned() -> None:
    path = _REPO / "infra" / "grafana" / "provisioning" / "alerting" / "usan_alerts.yml"
    assert path.is_file(), "usan_alerts.yml alert provisioning file missing"
    assert "usan-dial-slots-exhausted" in path.read_text()
