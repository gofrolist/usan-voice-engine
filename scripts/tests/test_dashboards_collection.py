import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_contract import (  # noqa: E402
    DASHBOARDS_DIR,
    EXPECTED,
    load_dashboard,
    validate_dashboard,
)


def test_all_expected_dashboards_present_and_valid():
    for filename, expected_uid in EXPECTED.items():
        path = DASHBOARDS_DIR / filename
        assert path.exists(), f"missing dashboard {filename}"
        doc = load_dashboard(filename)
        assert doc["uid"] == expected_uid
        assert validate_dashboard(doc) == [], f"{filename} failed contract"


def test_uids_and_titles_globally_unique():
    docs = [load_dashboard(name) for name in EXPECTED]
    uids = [d["uid"] for d in docs]
    titles = [d["title"] for d in docs]
    assert len(uids) == len(set(uids)), "dashboard uids must be unique"
    assert len(titles) == len(set(titles)), "dashboard titles must be unique"


def test_no_unexpected_json_files_in_dashboards_dir():
    on_disk = {p.name for p in DASHBOARDS_DIR.glob("*.json")}
    assert on_disk == set(EXPECTED), (
        f"unexpected dashboard files: {on_disk ^ set(EXPECTED)}"
    )
