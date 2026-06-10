import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    gridpos_overlaps,
    iter_panels,
    load_dashboard,
    validate_dashboard,
)


def test_system_dashboard_is_valid():
    doc = load_dashboard("system.json")
    assert validate_dashboard(doc) == []


def test_system_uid_and_datasources():
    doc = load_dashboard("system.json")
    assert doc["uid"] == "usan-system"
    for p in iter_panels(doc):
        if p.get("type") in ("row", "text"):
            continue
        assert p["datasource"]["uid"] in ("prometheus", "cloud-monitoring")


def test_system_uses_red_and_custom_metrics():
    doc = load_dashboard("system.json")
    exprs = " ".join(
        t.get("expr", "") for p in iter_panels(doc) for t in p.get("targets", [])
    )
    assert "http_requests_total" in exprs
    assert "http_request_duration_seconds_bucket" in exprs
    assert "histogram_quantile(0.95" in exprs
    assert 'status="5xx"' in exprs
    assert "usan_webhooks_total" in exprs
    assert "usan_tool_calls_total" in exprs
    assert 'up{job="usan-api"}' in exprs


def test_system_has_host_metric_panels():
    doc = load_dashboard("system.json")
    cm_panels = [
        p
        for p in iter_panels(doc)
        if (p.get("datasource") or {}).get("uid") == "cloud-monitoring"
    ]
    assert len(cm_panels) >= 3, "expected CPU/mem/disk host panels via cloud-monitoring"
    for p in cm_panels:
        assert p["datasource"]["type"] == "stackdriver"
    blob = json.dumps(doc)
    assert "compute.googleapis.com/instance/cpu/utilization" in blob
    assert "agent.googleapis.com/memory/percent_used" in blob
    assert "agent.googleapis.com/disk/percent_used" in blob


def test_system_no_overlap():
    doc = load_dashboard("system.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []


def test_system_has_followup_flags_panel():
    # Phase-3 B9 (F3): id 11 at y=29, the urgent-flags series.
    doc = load_dashboard("system.json")
    panel = next((p for p in iter_panels(doc) if p.get("id") == 11), None)
    assert panel is not None, "expected follow-up-flags panel id 11"
    assert panel["gridPos"] == {"x": 0, "y": 29, "w": 24, "h": 8}
    exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
    assert "usan_followup_flags_total" in exprs
    # The panel must surface the urgent series (alert rule fires on severity="urgent").
    assert 'severity="urgent"' in exprs


def test_system_dashboard_still_valid_with_followup_panel():
    doc = load_dashboard("system.json")
    assert validate_dashboard(doc) == []
    assert gridpos_overlaps(list(iter_panels(doc))) == []


def test_system_has_sms_failed_panel():
    doc = load_dashboard("system.json")
    panels = list(iter_panels(doc))
    sms = next((p for p in panels if p.get("id") == 12), None)
    assert sms is not None, "expected SMS-failed panel id 12 (Part D)"
    assert sms["gridPos"] == {"x": 0, "y": 37, "w": 12, "h": 8}
    exprs = " ".join(t.get("expr", "") for t in sms.get("targets", []))
    assert "usan_sms_messages_total" in exprs
    assert 'status="failed"' in exprs
    # still no overlap after the new panel
    assert gridpos_overlaps(panels) == []
    assert validate_dashboard(doc) == []
