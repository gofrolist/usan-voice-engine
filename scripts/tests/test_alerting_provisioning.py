"""Contract tests for the provisioned Grafana alert rules (usan_alerts.yml)."""

from pathlib import Path

import yaml

_ALERTS_PATH = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "grafana"
    / "provisioning"
    / "alerting"
)


def _rules() -> list[dict]:
    doc = yaml.safe_load((_ALERTS_PATH / "usan_alerts.yml").read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_alert_rules_present():
    uids = {rule["uid"] for rule in _rules()}
    assert {"usan-urgent-followup-flag", "usan-sms-delivery-failed"} <= uids


def test_alert_rules_route_to_provisioned_contact_point():
    doc = yaml.safe_load((_ALERTS_PATH / "usan_alerts.yml").read_text())
    receivers = {cp["name"] for cp in doc["contactPoints"]}
    for policy in doc["policies"]:
        assert policy["receiver"] in receivers


def test_alert_rules_handle_nodata_and_exec_errors():
    # File provisioning defaults to noDataState=NoData (which routes to the contact
    # point) and execErrState=Alerting (the UI/API default is Error). For these
    # low-traffic increase() queries, "no data" is the normal steady state (fresh
    # deploy, restart, quiet day) — it must not page, hence OK. Evaluation errors
    # must page so a broken rule cannot fail silent; pinning Alerting explicitly
    # guards against a Grafana default change.
    for rule in _rules():
        assert rule.get("noDataState") == "OK", rule["uid"]
        assert rule.get("execErrState") == "Alerting", rule["uid"]
