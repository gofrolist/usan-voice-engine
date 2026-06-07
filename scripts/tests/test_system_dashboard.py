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


def test_system_uid_and_prometheus_for_data_panels():
    doc = load_dashboard("system.json")
    assert doc["uid"] == "usan-system"
    for p in iter_panels(doc):
        if p.get("type") in ("row", "text"):
            continue
        assert p["datasource"]["uid"] == "prometheus"


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


def test_system_documents_host_metrics_deviation():
    doc = load_dashboard("system.json")
    text_panels = [p for p in iter_panels(doc) if p.get("type") == "text"]
    assert text_panels, "expected a text panel about host metrics"
    blob = " ".join(
        p.get("options", {}).get("content", "") for p in text_panels
    ).lower()
    assert "cloud monitoring" in blob


def test_system_no_overlap():
    doc = load_dashboard("system.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
