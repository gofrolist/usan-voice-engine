import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    gridpos_overlaps,
    iter_panels,
    load_dashboard,
    validate_dashboard,
)


def test_latency_dashboard_is_valid():
    doc = load_dashboard("latency.json")
    assert validate_dashboard(doc) == []


def test_latency_uid_and_datasource():
    doc = load_dashboard("latency.json")
    assert doc["uid"] == "usan-latency"
    panels = [p for p in iter_panels(doc) if p.get("type") not in ("row", "text")]
    assert panels, "expected data panels"
    for p in panels:
        assert p["datasource"]["uid"] == "postgres-ro"


def test_latency_queries_turn_metrics_and_target_line():
    doc = load_dashboard("latency.json")
    sql = " ".join(
        t.get("rawSql", "") for p in iter_panels(doc) for t in p.get("targets", [])
    )
    assert "turn_metrics" in sql
    assert "response_latency_ms" in sql
    assert "percentile_cont(0.95)" in sql
    # 1200 ms target line lives in a panel threshold step.
    steps = [
        s.get("value")
        for p in iter_panels(doc)
        for s in p.get("fieldConfig", {})
        .get("defaults", {})
        .get("thresholds", {})
        .get("steps", [])
    ]
    assert 1200 in steps


def test_latency_no_overlap():
    doc = load_dashboard("latency.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
