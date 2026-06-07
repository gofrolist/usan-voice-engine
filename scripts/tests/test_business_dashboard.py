import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    gridpos_overlaps,
    iter_panels,
    load_dashboard,
    validate_dashboard,
)


def test_business_dashboard_is_valid():
    doc = load_dashboard("business.json")
    assert validate_dashboard(doc) == []


def test_business_uid_and_postgres_only():
    doc = load_dashboard("business.json")
    assert doc["uid"] == "usan-business"
    for p in iter_panels(doc):
        if p.get("type") in ("row", "text"):
            continue
        assert p["datasource"]["uid"] == "postgres-ro"


def test_business_covers_outcomes_wellness_and_meds():
    doc = load_dashboard("business.json")
    sql = " ".join(
        t.get("rawSql", "") for p in iter_panels(doc) for t in p.get("targets", [])
    )
    assert "wellness_logs" in sql and "medication_logs" in sql
    # outcome rates use the lowercase enum values from calls.status
    assert "'completed'" in sql
    assert "'no_answer'" in sql
    # adherence uses the boolean taken column
    assert "taken" in sql
    # mood & pain
    assert "mood" in sql and "pain_level" in sql
    # never select PHI free-text / names; this dashboard must not join elders at all
    assert "notes" not in sql and "e.name" not in sql and "elders" not in sql


def test_business_no_overlap():
    doc = load_dashboard("business.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
