import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    gridpos_overlaps,
    iter_panels,
    load_dashboard,
    validate_dashboard,
)


def test_cost_dashboard_is_valid():
    doc = load_dashboard("cost.json")
    assert validate_dashboard(doc) == []


def test_cost_uid_and_postgres_only():
    doc = load_dashboard("cost.json")
    assert doc["uid"] == "usan-cost"
    for p in iter_panels(doc):
        if p.get("type") in ("row", "text"):
            continue
        assert p["datasource"]["uid"] == "postgres-ro"


def test_cost_has_retell_baseline_constant_variable():
    doc = load_dashboard("cost.json")
    variables = doc["templating"]["list"]
    names = {v["name"]: v for v in variables}
    assert "retell_baseline" in names
    assert names["retell_baseline"]["type"] == "constant"


def test_cost_queries_call_metrics_components_and_per_elder():
    doc = load_dashboard("cost.json")
    sql = " ".join(
        t.get("rawSql", "") for p in iter_panels(doc) for t in p.get("targets", [])
    )
    assert "call_metrics" in sql
    for col in (
        "cost_total_usd",
        "cost_telephony_usd",
        "cost_llm_usd",
        "cost_tts_usd",
        "cost_stt_usd",
    ):
        assert col in sql
    assert "${retell_baseline}" in sql
    # per-elder keyed on external_id, never name (PHI).
    assert "external_id" in sql
    assert "e.name" not in sql and "elders.name" not in sql


def test_cost_no_overlap():
    doc = load_dashboard("cost.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
