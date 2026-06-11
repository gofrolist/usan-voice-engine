"""Infra contract: the 4 WEBHOOK_DELIVERY_* keys are plumbed through compose + env examples.

Clone of test_infra_scheduler_env.py for outbound event webhooks (spec
2026-06-10 §5.1/§10.14/§11): dev compose defaults the delivery flag ON so
POST /v1/webhook-endpoints/{id}/test works in every dev stack; the prod
overlay re-pins it OFF so a stale VM .env interpolates to the safe/inert
state. Also pins the two provisioned alert-rule uids — the circuit breaker
can permanently mute the delivery-failed alert (disabled endpoints' rows are
never claimed), so the trip itself must page (spec §9).
"""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_KEYS = (
    "WEBHOOK_DELIVERY_ENABLED",
    "WEBHOOK_DELIVERY_POLL_INTERVAL_S",
    "WEBHOOK_DELIVERY_TIMEOUT_S",
    "WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD",
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


def test_compose_api_service_has_webhook_env() -> None:
    env = _api_env("docker-compose.yml")
    for k in _KEYS:
        assert k in env, f"{k} missing from api service environment"


def test_dev_compose_enables_delivery_flag() -> None:
    env = _api_env("docker-compose.yml")
    # Dev compose defaults the delivery flag ON (scheduler precedent — otherwise
    # POST /v1/webhook-endpoints/{id}/test 409s in every dev stack, spec §10.14).
    assert env["WEBHOOK_DELIVERY_ENABLED"] == "${WEBHOOK_DELIVERY_ENABLED:-true}"


def test_prod_overlay_pins_flag_off() -> None:
    env = _api_env("docker-compose.prod.yml")
    # The prod overlay's environment map overrides the dev-on base file, so a
    # stale /opt/usan/infra/.env interpolates the default => safe/off (spec §11.1).
    assert env["WEBHOOK_DELIVERY_ENABLED"] == "${WEBHOOK_DELIVERY_ENABLED:-false}"


def test_env_example_keys_commented_with_inbound_outbound_block() -> None:
    text = (_REPO / "infra" / ".env.example").read_text()
    for k in _KEYS:
        # Commented-defaults house style (retry-orchestrator block precedent).
        assert f"# {k}=" in text, f"{k} missing from .env.example as a commented default"
    # The inbound-vs-outbound WEBHOOK_* disambiguation block (spec §5.1) — stable sentinel.
    assert "WEBHOOK_MAX_AGE_S above is the INBOUND LiveKit" in text


def test_env_prod_example_pins_false() -> None:
    text = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.prod.example"
    # Ship inert (spec §11.1): the delivery flag is live-value OFF in prod.
    assert "WEBHOOK_DELIVERY_ENABLED=false" in text


def test_alert_rule_uids_in_env_contract() -> None:
    path = _REPO / "infra" / "grafana" / "provisioning" / "alerting" / "usan_alerts.yml"
    text = path.read_text()
    assert "usan-webhook-delivery-failed" in text
    assert "usan-webhook-endpoint-auto-disabled" in text
