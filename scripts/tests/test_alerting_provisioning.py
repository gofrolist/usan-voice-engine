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
    assert {
        "usan-urgent-followup-flag",
        "usan-sms-delivery-failed",
        "usan-dial-slots-exhausted",
        "usan-webhook-delivery-failed",
        "usan-webhook-endpoint-auto-disabled",
    } <= uids


def test_webhook_alert_rules_shape():
    # Both outbound-webhook rules fire immediately (for: 0m) at warning severity
    # over a 30m increase() window. Two rules are load-bearing: a tripped circuit
    # breaker MUTES the delivery-failed alert (disabled endpoints' rows are never
    # claimed, so they never reach outcome="failed") — the trip itself must page
    # (spec 2026-06-10 §9 / runbook §11.5).
    by_uid = {rule["uid"]: rule for rule in _rules()}
    expected_exprs = {
        "usan-webhook-delivery-failed": 'usan_webhook_deliveries_total{outcome="failed"}',
        "usan-webhook-endpoint-auto-disabled": "usan_webhook_endpoints_auto_disabled_total",
    }
    for uid, expr_fragment in expected_exprs.items():
        rule = by_uid[uid]
        assert rule["for"] == "0m", uid
        assert rule["labels"]["severity"] == "warning", uid
        query = next(item for item in rule["data"] if item["refId"] == "A")
        assert query["datasourceUid"] == "prometheus", uid
        assert expr_fragment in query["model"]["expr"], uid
        assert "[30m]" in query["model"]["expr"], uid
        assert query["relativeTimeRange"]["from"] == 1800, uid


def test_dial_slots_alert_sustained_ten_minutes():
    # usan_dial_slots_free == 0 must be SUSTAINED for 10m before paging: a brief
    # zero is normal under load; ten minutes of no free slots means autonomous
    # dialing (schedules, batches, retries) has stopped.
    rule = next(r for r in _rules() if r["uid"] == "usan-dial-slots-exhausted")
    assert rule["for"] == "10m"
    assert rule["labels"]["severity"] == "page"
    by_ref = {item["refId"]: item for item in rule["data"]}
    assert by_ref["A"]["datasourceUid"] == "prometheus"
    evaluator = by_ref["C"]["model"]["conditions"][0]["evaluator"]
    assert evaluator["type"] == "lt"
    assert evaluator["params"] == [1]


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
