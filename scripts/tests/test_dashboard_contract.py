import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    ALLOWED_DS_UIDS,
    gridpos_overlaps,
    iter_panels,
    validate_dashboard,
)


def _good_doc():
    """A minimal dashboard that satisfies every contract rule."""
    return {
        "id": None,
        "uid": "usan-sample",
        "title": "Sample",
        "schemaVersion": 39,
        "tags": ["usan"],
        "time": {"from": "now-24h", "to": "now"},
        "timezone": "",
        "refresh": "1m",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "A SQL panel",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
                "datasource": {"type": "postgres", "uid": "postgres-ro"},
                "targets": [
                    {
                        "refId": "A",
                        "datasource": {"type": "postgres", "uid": "postgres-ro"},
                        "format": "time_series",
                        "rawQuery": True,
                        "editorMode": "code",
                        "rawSql": "SELECT 1 AS time, 2 AS v",
                    }
                ],
                "fieldConfig": {"defaults": {}, "overrides": []},
                "options": {},
            },
            {
                "id": 2,
                "type": "timeseries",
                "title": "A Prometheus panel",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [
                    {
                        "refId": "A",
                        "datasource": {"type": "prometheus", "uid": "prometheus"},
                        "expr": "up",
                        "range": True,
                    }
                ],
                "fieldConfig": {"defaults": {}, "overrides": []},
                "options": {},
            },
        ],
    }


def test_good_doc_has_no_errors():
    assert validate_dashboard(_good_doc()) == []


def test_allowed_ds_uids_are_exactly_the_provisioned_set():
    assert ALLOWED_DS_UIDS == {"prometheus", "postgres-ro", "cloud-monitoring"}


def test_rejects_non_null_top_level_id():
    doc = _good_doc()
    doc["id"] = 7
    assert any("id" in e for e in validate_dashboard(doc))


def test_rejects_missing_or_low_schema_version():
    doc = _good_doc()
    doc["schemaVersion"] = 12
    assert any("schemaVersion" in e for e in validate_dashboard(doc))
    del doc["schemaVersion"]
    assert any("schemaVersion" in e for e in validate_dashboard(doc))


def test_rejects_bad_uid():
    doc = _good_doc()
    doc["uid"] = "USAN Sample!"  # spaces + uppercase + punctuation
    assert any("uid" in e for e in validate_dashboard(doc))


def test_rejects_inputs_and_requires_keys():
    doc = _good_doc()
    doc["__inputs"] = [{"name": "DS_PROM"}]
    errs = validate_dashboard(doc)
    assert any("__inputs" in e for e in errs)


def test_rejects_unknown_datasource_uid():
    doc = _good_doc()
    doc["panels"][0]["datasource"]["uid"] = "some-other-ds"
    assert any("datasource" in e for e in validate_dashboard(doc))


def test_rejects_postgres_target_without_raw_sql():
    doc = _good_doc()
    doc["panels"][0]["targets"][0].pop("rawSql")
    assert any("rawSql" in e for e in validate_dashboard(doc))


def test_rejects_prometheus_target_without_expr():
    doc = _good_doc()
    doc["panels"][1]["targets"][0].pop("expr")
    assert any("expr" in e for e in validate_dashboard(doc))


def test_rejects_duplicate_panel_ids():
    doc = _good_doc()
    doc["panels"][1]["id"] = 1  # collide with panel 0
    assert any("panel id" in e for e in validate_dashboard(doc))


def test_detects_gridpos_overlap():
    panels = [
        {"id": 1, "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0}},
        {"id": 2, "gridPos": {"h": 8, "w": 12, "x": 6, "y": 0}},  # overlaps panel 1
    ]
    assert gridpos_overlaps(panels)


def test_no_overlap_for_side_by_side():
    panels = [
        {"id": 1, "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0}},
        {"id": 2, "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0}},
    ]
    assert not gridpos_overlaps(panels)


def test_iter_panels_descends_into_rows():
    doc = {
        "panels": [
            {
                "id": 10,
                "type": "row",
                "title": "R",
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
                "panels": [
                    {
                        "id": 11,
                        "type": "stat",
                        "title": "inner",
                        "gridPos": {"h": 4, "w": 6, "x": 0, "y": 1},
                    }
                ],
            },
        ]
    }
    ids = {p["id"] for p in iter_panels(doc)}
    assert ids == {10, 11}


def test_text_panel_without_datasource_is_allowed():
    doc = _good_doc()
    doc["panels"].append(
        {
            "id": 3,
            "type": "text",
            "title": "note",
            "gridPos": {"h": 4, "w": 24, "x": 0, "y": 8},
            "options": {"content": "host metrics live in Cloud Monitoring"},
        }
    )
    assert validate_dashboard(doc) == []


def test_rejects_requires_key():
    doc = _good_doc()
    doc["__requires"] = [{"type": "datasource"}]
    assert any("__requires" in e for e in validate_dashboard(doc))


def test_string_form_datasource_is_flagged_not_crashed():
    doc = _good_doc()
    doc["panels"][0]["datasource"] = "postgres-ro"  # legacy string form
    errs = validate_dashboard(doc)
    assert any("datasource" in e for e in errs)


def test_validate_dashboard_surfaces_gridpos_overlap():
    doc = _good_doc()
    doc["panels"][1]["gridPos"] = {"h": 8, "w": 12, "x": 6, "y": 0}  # overlaps panel 1
    assert any("overlap" in e for e in validate_dashboard(doc))


def test_rejects_bool_schema_version():
    doc = _good_doc()
    doc["schemaVersion"] = True
    assert any("schemaVersion" in e for e in validate_dashboard(doc))
