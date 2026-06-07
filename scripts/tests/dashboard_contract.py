"""Pure-stdlib structural validator for provisioned Grafana dashboard JSON.

Runs in the `pytest (scripts)` CI job, which has only Python 3.12 + pytest.
No third-party imports. The contract encodes the rules from
docs/superpowers/plans/2026-06-07-plan-mon-3-grafana-dashboards.md so that a
malformed or PHI-risky dashboard fails CI before it can reach Grafana.
"""

import json
import re
from pathlib import Path
from typing import Any

# The two datasources MON-2 provisioned (infra/grafana/provisioning/datasources).
ALLOWED_DS_UIDS = {"prometheus", "postgres-ro"}

# Repo-root-relative location of the dashboard JSON the file provider loads.
DASHBOARDS_DIR = (
    Path(__file__).resolve().parents[2] / "infra" / "grafana" / "dashboards"
)

# The dashboards MON-3 must ship: filename -> required stable uid.
EXPECTED = {
    "latency.json": "usan-latency",
    "cost.json": "usan-cost",
    "business.json": "usan-business",
    "system.json": "usan-system",
}

_UID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_MIN_SCHEMA_VERSION = 39
_GRID_WIDTH = 24


def load_dashboard(name: str) -> dict[str, Any]:
    """Load and JSON-parse a dashboard file by basename (e.g. 'latency.json')."""
    path = DASHBOARDS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Dashboard file not found: {path}")
    return json.loads(path.read_text())


def iter_panels(doc: dict[str, Any]):
    """Yield every panel, descending one level into any panel's nested `panels`
    (e.g. the children of a collapsed row)."""
    for panel in doc.get("panels", []) or []:
        yield panel
        for child in panel.get("panels", []) or []:
            yield child


def _rects_overlap(a: dict, b: dict) -> bool:
    ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
    bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
    # No overlap if one is entirely left/right/above/below the other.
    if ax + aw <= bx or bx + bw <= ax:
        return False
    if ay + ah <= by or by + bh <= ay:
        return False
    return True


def gridpos_overlaps(panels: list[dict]) -> list[tuple[Any, Any]]:
    """Return pairs of panel ids whose gridPos rectangles overlap."""
    boxes = [(p.get("id"), p["gridPos"]) for p in panels if "gridPos" in p]
    clashes = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _rects_overlap(boxes[i][1], boxes[j][1]):
                clashes.append((boxes[i][0], boxes[j][0]))
    return clashes


def _target_ds(target: dict, panel: dict) -> dict | None:
    td = target.get("datasource")
    return td if td is not None else panel.get("datasource")


def validate_dashboard(doc: dict[str, Any]) -> list[str]:
    """Return a list of human-readable contract violations ([] == valid)."""
    errors: list[str] = []

    # --- top-level ---------------------------------------------------------
    if "id" in doc and doc["id"] is not None:
        errors.append("top-level 'id' must be null for provisioned dashboards")
    for forbidden in ("__inputs", "__requires"):
        if forbidden in doc:
            errors.append(
                f"forbidden key '{forbidden}' (hardcode datasource uids instead)"
            )

    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("'title' must be a non-empty string")

    uid = doc.get("uid")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        errors.append(f"'uid' must match {_UID_RE.pattern} (got {uid!r})")

    sv = doc.get("schemaVersion")
    if not isinstance(sv, int) or isinstance(sv, bool) or sv < _MIN_SCHEMA_VERSION:
        errors.append(
            f"'schemaVersion' must be an int >= {_MIN_SCHEMA_VERSION} (got {sv!r})"
        )

    panels = doc.get("panels")
    if not isinstance(panels, list) or not panels:
        errors.append("'panels' must be a non-empty list")
        return errors  # nothing more to check

    # --- panels ------------------------------------------------------------
    seen_ids: set[Any] = set()
    for panel in iter_panels(doc):
        pid = panel.get("id")
        label = f"panel {pid!r} ({panel.get('title', '?')})"
        if pid is None:
            errors.append(f"{label}: every panel needs an integer 'id'")
        elif pid in seen_ids:
            errors.append(f"duplicate panel id {pid!r}")
        else:
            seen_ids.add(pid)

        if not isinstance(panel.get("title"), str):
            errors.append(f"{label}: 'title' must be a string")

        grid = panel.get("gridPos")
        if not (
            isinstance(grid, dict)
            and all(isinstance(grid.get(k), int) for k in ("h", "w", "x", "y"))
        ):
            errors.append(f"{label}: 'gridPos' must have int h/w/x/y")
        elif grid["x"] + grid["w"] > _GRID_WIDTH:
            errors.append(f"{label}: gridPos x+w exceeds grid width {_GRID_WIDTH}")

        ptype = panel.get("type")
        # 'row' and 'text' panels carry no datasource/targets.
        if ptype in ("row", "text"):
            continue

        pds = panel.get("datasource")
        if pds is not None:
            if not isinstance(pds, dict) or pds.get("uid") not in ALLOWED_DS_UIDS:
                errors.append(
                    f"{label}: datasource must be a dict with uid in {ALLOWED_DS_UIDS}"
                )

        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"{label}: data panel must have a non-empty 'targets' list")
            continue
        for t in targets:
            tds = _target_ds(t, panel)
            tds_uid = tds.get("uid") if isinstance(tds, dict) else None
            tds_type = tds.get("type") if isinstance(tds, dict) else None
            if tds_uid not in ALLOWED_DS_UIDS:
                errors.append(
                    f"{label} target {t.get('refId')}: datasource uid "
                    f"{tds_uid!r} not in {ALLOWED_DS_UIDS}"
                )
            if not t.get("refId"):
                errors.append(f"{label}: a target is missing 'refId'")
            if tds_type == "postgres" and not str(t.get("rawSql", "")).strip():
                errors.append(
                    f"{label} target {t.get('refId')}: postgres target needs 'rawSql'"
                )
            if tds_type == "prometheus" and not str(t.get("expr", "")).strip():
                errors.append(
                    f"{label} target {t.get('refId')}: prometheus target needs 'expr'"
                )

    # Row panels are included intentionally: a content panel overlapping a row
    # header is a real layout bug worth flagging.
    for a, b in gridpos_overlaps(list(iter_panels(doc))):
        errors.append(f"gridPos overlap between panels {a!r} and {b!r}")

    return errors
