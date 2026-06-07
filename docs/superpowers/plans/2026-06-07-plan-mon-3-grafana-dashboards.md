# MON-3 — Grafana Dashboards-as-Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the four Grafana dashboards the monitoring spec defers to phase 5 (Latency, Cost, Business/Care, System) as version-controlled JSON loaded by the provisioning MON-2 already stood up.

**Architecture:** Purely additive. MON-2 provisioned two datasources (`prometheus`, `postgres-ro`) and a file-based dashboard provider that loads `*.json` from `/var/lib/grafana/dashboards` (host bind `infra/grafana/dashboards/`, folder `USAN`, 30 s rescan, `allowUiUpdates=false`). This plan drops four hand-authored dashboard JSON files into that directory and guards them with a pure-stdlib structural validator that runs in the existing `pytest (scripts)` CI job. No app code, no migrations, no Terraform, no compose, no Caddy changes. `build.yml` already ships `infra/grafana` recursively, so the files reach the VM on the next `v*` tag with zero workflow edits.

**Tech Stack:** Grafana 12.4.4 dashboard JSON model (schemaVersion ≥ 39, datasource refs by uid); Grafana Postgres datasource macros (`$__timeFilter`, `$__timeGroupAlias`) over PG18 (`percentile_cont`, `FILTER`); PromQL over `prometheus-fastapi-instrumentator` 8.0.0 RED metrics + the three `usan_*_total` counters; Python 3.12 stdlib + pytest for validation.

---

## Scope & boundaries

**In scope (spec §9 dashboard catalog + §12 phase 5):**

- `infra/grafana/dashboards/latency.json` — Postgres, over `turn_metrics`.
- `infra/grafana/dashboards/cost.json` — Postgres, over `call_metrics` (+ `calls`/`elders` for per-elder).
- `infra/grafana/dashboards/business.json` — Postgres, over `calls`/`wellness_logs`/`medication_logs`.
- `infra/grafana/dashboards/system.json` — Prometheus RED + custom counters.
- A structural validator + tests in `scripts/tests/` (runs in the existing `pytest (scripts)` CI job).
- A runbook section in `infra/README.md`.

**Out of scope (do NOT touch):**

- Any file under `apps/api/`, `services/agent/`, `infra/terraform/`, `infra/docker-compose*.yml`, `infra/Caddyfile`, `infra/prometheus/`, `infra/grafana/provisioning/`, `.github/workflows/`. The datasources, provider, scrape config, secrets, CIDR gate, and deploy wiring all shipped in MON-1/MON-2 and are correct as-is.
- New alerting, Loki/log panels, OpenTelemetry — spec §13 out-of-scope.

## Reconciliations / deliberate deviations from the spec

The spec was written before MON-1/MON-2 landed. These are intentional, verified deviations — call them out to reviewers so they are not flagged as gaps.

1. **System host metrics (CPU/mem/disk) are omitted.** Spec §9 lists them under "System (RED) | Prometheus + Cloud Monitoring", but §9 also says the Cloud Monitoring datasource is *optional*, and MON-2 did **not** provision one (`infra/grafana/provisioning/datasources/datasources.yml` has only `prometheus` + `postgres-ro`). Wiring the Google Cloud Monitoring datasource + `roles/monitoring.viewer` is a separate infra change. The System dashboard is therefore **Prometheus-only**, and includes a `text` panel pointing operators to the GCP Cloud Monitoring console for host metrics. Adding the datasource later is a one-line follow-up that these panels can then consume.

2. **`usan_calls_total` in practice only carries `end_reason="completed"`.** The counter's only increment site is `end_call` gated on a successful `complete_call_if_in_progress` (which always sets `status=completed`). PromQL on the System dashboard uses `by (direction, end_reason)` so it stays correct if other terminal reasons are wired later, but reviewers should not expect `no_answer`/`failed` series today. Per-outcome **business** rates come from the Postgres `calls.status` column (Business dashboard), which is fully populated — that is the authoritative outcome source, exactly as the spec intends (§3 "Postgres is the workhorse").

3. **RetellAI baseline is a dashboard constant, not read from API config.** Spec §6 mentions a `retell_baseline_per_min` API constant. Grafana cannot read API settings; the Cost dashboard exposes a Grafana **constant template variable** `retell_baseline` (default `0.10`) rendered as a flat comparison series. Re-pricing = edit the variable default in `cost.json` (still GitOps).

4. **Per-elder cost is keyed by `elders.external_id`, not `elders.name`.** Spec §10 permits per-elder drill-downs behind the operator-CIDR + auth + TLS gate, but §10 also says "No PHI is logged into panel titles/annotations". A resident's name is PHI; their external business key is not. The Cost "per elder" table shows `COALESCE(external_id, left(id::text,8))` labelled `elder` — identifiable to an operator, no name surfaced.

5. **Status enum strings are matched verbatim from the DB.** Grafana SQL filters use the lowercase PG enum *values* (`'completed'`, `'no_answer'`, `'voicemail_left'`, `'failed'`, `'dnc_blocked'`, …), because `calls.status`/`calls.direction` store `enum.value` (see `db/base.py` + `values_callable=_enum_values`).

## Verified facts (the queries depend on these — confirmed first-hand this session)

- **Datasource uids:** `prometheus` (Prometheus, default) and `postgres-ro` (Postgres, DB `usan`, role `grafana_ro`, `postgresVersion: 1500`). Reference them in panel/target JSON as `{"type":"postgres","uid":"postgres-ro"}` / `{"type":"prometheus","uid":"prometheus"}`.
- **Provider:** folder `USAN`, container path `/var/lib/grafana/dashboards`, host bind `infra/grafana/dashboards/`, 30 s rescan, `allowUiUpdates=false`, `foldersFromFilesStructure=false` (all JSON lands in `USAN` regardless of subdir).
- **`turn_metrics`** (PHI-free): `call_id` (uuid), `turn_index` (int), `eou_delay_ms`, `transcription_delay_ms`, `stt_duration_ms`, `llm_ttft_ms`, `tts_ttfb_ms`, `llm_completion_tokens`, `tts_characters`, `response_latency_ms` (all nullable int), `created_at` (timestamptz, indexed).
- **`call_metrics`** (PHI-free, 1:1 with calls): `call_id` (pk), `llm_prompt_tokens`, `llm_completion_tokens`, `llm_total_tokens`, `tts_characters` (int), `stt_audio_seconds` numeric(10,2), `duration_seconds` (int, nullable), `cost_telephony_usd`, `cost_llm_usd`, `cost_stt_usd`, `cost_tts_usd`, `cost_storage_usd`, `cost_total_usd` (numeric(12,6)), `pricing_version` (text), `created_at` (timestamptz, indexed).
- **`calls`:** `id`, `elder_id` (uuid, nullable, FK→elders ON DELETE SET NULL), `direction` (enum value), `status` (enum value), `attempt` (smallint), `duration_seconds` (int, nullable), `created_at` (timestamptz). Status values: `queued, dialing, ringing, in_progress, completed, voicemail_left, no_answer, busy, failed, dnc_blocked, cancelled`. Direction values: `outbound, inbound`.
- **`elders`:** `id` (uuid), `external_id` (text, nullable, unique), `name` (text — PHI, do not surface).
- **`wellness_logs`:** `elder_id`, `call_id`, `mood` (smallint, nullable), `pain_level` (smallint, nullable), `logged_at` (timestamptz). **`medication_logs`:** `taken` (bool), `medication_name` (text), `logged_at` (timestamptz).
- **Prometheus surface (scraped names):** custom counters `usan_calls_total{direction,end_reason}`, `usan_webhooks_total{type,outcome}`, `usan_tool_calls_total{tool,outcome}`. Default RED: `http_requests_total{method,status,handler}` (status grouped → `2xx/3xx/4xx/5xx`), `http_request_duration_seconds_bucket{le,method,status,handler}` (+ `_sum`/`_count`), `http_request_duration_highr_seconds_bucket{le}` (no handler/method/status). `/metrics` + `/health` excluded. Scrape jobs: `usan-api` (label `service=api`) and `prometheus`.
- **CI:** `pytest (scripts)` job = `actions/setup-python@v5` (3.12) → `pip install pytest` → `python -m pytest scripts/tests -v`. **No uv, no third-party libs** — the validator must be pure stdlib (`json`, `pathlib`).
- **Deploy:** `build.yml` scp `source:` already includes `infra/grafana` (whole tree, recursive). New dashboard files ship automatically on the next `v*` tag. **No `build.yml` change in this plan.**

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `scripts/tests/dashboard_contract.py` | Pure-stdlib validator: `validate_dashboard(doc)`, `iter_panels`, `gridpos_overlaps`, `load_dashboard`, constants (`ALLOWED_DS_UIDS`, `DASHBOARDS_DIR`, `EXPECTED`) | Create (Task 1) |
| `scripts/tests/test_dashboard_contract.py` | Unit tests for the validator using inline good/bad fixtures (no real dashboards) | Create (Task 1) |
| `infra/grafana/dashboards/latency.json` | Latency dashboard (Postgres / `turn_metrics`) | Create (Task 2) |
| `scripts/tests/test_latency_dashboard.py` | Loads `latency.json`, runs validator + Latency-specific assertions | Create (Task 2) |
| `infra/grafana/dashboards/cost.json` | Cost dashboard (Postgres / `call_metrics`) | Create (Task 3) |
| `scripts/tests/test_cost_dashboard.py` | Loads `cost.json`, runs validator + Cost-specific assertions | Create (Task 3) |
| `infra/grafana/dashboards/business.json` | Business/Care dashboard (Postgres / `calls`+wellness+meds) | Create (Task 4) |
| `scripts/tests/test_business_dashboard.py` | Loads `business.json`, runs validator + Business-specific assertions | Create (Task 4) |
| `infra/grafana/dashboards/system.json` | System/RED dashboard (Prometheus) | Create (Task 5) |
| `scripts/tests/test_system_dashboard.py` | Loads `system.json`, runs validator + System-specific assertions | Create (Task 5) |
| `scripts/tests/test_dashboards_collection.py` | Cross-file invariants: all 4 present, uids/titles globally unique | Create (Task 6) |
| `infra/README.md` | MON-3 runbook section (how dashboards deploy + verify) | Modify (Task 6) |

## Critical implementation gotchas

- **Provisioned dashboards must set top-level `"id": null`.** A stray numeric `id` (left over from a Grafana export) collides across instances. Always `null`; Grafana assigns its own. The validator enforces this.
- **No `__inputs` / `__requires`.** Those belong to the export-with-datasource-variable flow. Because our datasource uids are fixed (`prometheus`, `postgres-ro`), hardcode them in every panel/target and omit those keys. The validator forbids them.
- **`schemaVersion` ≥ 39, authored low on purpose.** Grafana migrates dashboards *forward* on load; authoring `39` is forward-compatible with 12.4.4. Do not chase the newest number — author `39`, let Grafana upgrade. The validator asserts `int >= 39`.
- **Postgres time-series panels:** first selected column is the time bucket via `$__timeGroupAlias(<ts_col>, $__interval)` (auto-aliases to `time`), then `GROUP BY 1 ORDER BY 1`. Wrap the range filter in `$__timeFilter(<ts_col>)`. Set target `"format": "time_series"`. Table/stat/barchart/histogram targets use `"format": "table"`.
- **Every Postgres target needs `"rawQuery": true` + `"rawSql"` + `"editorMode": "code"`;** every Prometheus target needs `"expr"` (+ `"range": true` for time series). The validator checks the type-appropriate field is present and non-empty.
- **`gridPos` must not overlap.** 24-wide grid; lay panels out with explicit non-overlapping `{h,w,x,y}`. The validator (and each dashboard test) runs an overlap check — copy the exact `gridPos` values from this plan.
- **PHI discipline:** never select `elders.name`, `elders.phone_e164`, `transcripts.*`, `wellness_logs.notes`, or free-text into any panel. Use `external_id`/aggregates only. The Postgres role (`grafana_ro`) is also physically denied `transcripts`/`dnc_list` (migration 0009), so a slip fails loudly rather than leaking.
- **Pure-stdlib tests.** The scripts CI job has only `pytest` + the 3.12 stdlib. Do **not** `import yaml`, `requests`, `jsonschema`, etc. Use `json` + `pathlib`.

---

## Task 1: Dashboard structural validator + unit tests

**Files:**
- Create: `scripts/tests/dashboard_contract.py`
- Test: `scripts/tests/test_dashboard_contract.py`

This task builds the reusable contract with pure TDD against inline fixtures — it does **not** depend on any real dashboard existing yet. Later tasks import `validate_dashboard` and feed it the real files.

- [ ] **Step 1: Write the failing unit tests**

Create `scripts/tests/test_dashboard_contract.py`:

```python
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


def test_allowed_ds_uids_are_exactly_the_two_provisioned():
    assert ALLOWED_DS_UIDS == {"prometheus", "postgres-ro"}


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
            {"id": 10, "type": "row", "title": "R", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
             "panels": [{"id": 11, "type": "stat", "title": "inner",
                         "gridPos": {"h": 4, "w": 6, "x": 0, "y": 1}}]},
        ]
    }
    ids = {p["id"] for p in iter_panels(doc)}
    assert ids == {10, 11}


def test_text_panel_without_datasource_is_allowed():
    doc = _good_doc()
    doc["panels"].append({
        "id": 3, "type": "text", "title": "note",
        "gridPos": {"h": 4, "w": 24, "x": 0, "y": 8},
        "options": {"content": "host metrics live in Cloud Monitoring"},
    })
    assert validate_dashboard(doc) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_dashboard_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard_contract'`.

- [ ] **Step 3: Write the validator module**

Create `scripts/tests/dashboard_contract.py`:

```python
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
DASHBOARDS_DIR = Path(__file__).resolve().parents[2] / "infra" / "grafana" / "dashboards"

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
    return json.loads((DASHBOARDS_DIR / name).read_text())


def iter_panels(doc: dict[str, Any]):
    """Yield every panel, descending one level into 'row' panels' nested panels."""
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
    return target.get("datasource") or panel.get("datasource")


def validate_dashboard(doc: dict[str, Any]) -> list[str]:
    """Return a list of human-readable contract violations ([] == valid)."""
    errors: list[str] = []

    # --- top-level ---------------------------------------------------------
    if "id" in doc and doc["id"] is not None:
        errors.append("top-level 'id' must be null for provisioned dashboards")
    for forbidden in ("__inputs", "__requires"):
        if forbidden in doc:
            errors.append(f"forbidden key '{forbidden}' (hardcode datasource uids instead)")

    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("'title' must be a non-empty string")

    uid = doc.get("uid")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        errors.append(f"'uid' must match {_UID_RE.pattern} (got {uid!r})")

    sv = doc.get("schemaVersion")
    if not isinstance(sv, int) or sv < _MIN_SCHEMA_VERSION:
        errors.append(f"'schemaVersion' must be an int >= {_MIN_SCHEMA_VERSION} (got {sv!r})")

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
        if not (isinstance(grid, dict) and all(
            isinstance(grid.get(k), int) for k in ("h", "w", "x", "y")
        )):
            errors.append(f"{label}: 'gridPos' must have int h/w/x/y")
        elif grid["x"] + grid["w"] > _GRID_WIDTH:
            errors.append(f"{label}: gridPos x+w exceeds grid width {_GRID_WIDTH}")

        ptype = panel.get("type")
        # 'row' and 'text' panels carry no datasource/targets.
        if ptype in ("row", "text"):
            continue

        pds = panel.get("datasource")
        if pds is not None and pds.get("uid") not in ALLOWED_DS_UIDS:
            errors.append(f"{label}: datasource uid {pds.get('uid')!r} not in {ALLOWED_DS_UIDS}")

        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"{label}: data panel must have a non-empty 'targets' list")
            continue
        for t in targets:
            tds = _target_ds(t, panel)
            tds_uid = tds.get("uid") if isinstance(tds, dict) else None
            tds_type = tds.get("type") if isinstance(tds, dict) else None
            if tds_uid not in ALLOWED_DS_UIDS:
                errors.append(f"{label} target {t.get('refId')}: datasource uid "
                              f"{tds_uid!r} not in {ALLOWED_DS_UIDS}")
            if not t.get("refId"):
                errors.append(f"{label}: a target is missing 'refId'")
            if tds_type == "postgres" and not str(t.get("rawSql", "")).strip():
                errors.append(f"{label} target {t.get('refId')}: postgres target needs 'rawSql'")
            if tds_type == "prometheus" and not str(t.get("expr", "")).strip():
                errors.append(f"{label} target {t.get('refId')}: prometheus target needs 'expr'")

    for a, b in gridpos_overlaps(list(iter_panels(doc))):
        errors.append(f"gridPos overlap between panels {a!r} and {b!r}")

    return errors
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_dashboard_contract.py -v`
Expected: PASS (all ~14 tests green).

- [ ] **Step 5: Commit**

```bash
git add scripts/tests/dashboard_contract.py scripts/tests/test_dashboard_contract.py
git commit -m "test(infra): add stdlib structural validator for Grafana dashboard JSON"
```

---

## Task 2: Latency dashboard (`latency.json`)

**Files:**
- Create: `infra/grafana/dashboards/latency.json`
- Test: `scripts/tests/test_latency_dashboard.py`

Source: Postgres `turn_metrics` (spec §7 + §9). Panels: response-latency p50/p95/p99 with a 1200 ms target line; per-stage p95; per-turn distribution; worst-calls table.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_latency_dashboard.py`:

```python
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
        for s in p.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps", [])
    ]
    assert 1200 in steps


def test_latency_no_overlap():
    doc = load_dashboard("latency.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest scripts/tests/test_latency_dashboard.py -v`
Expected: FAIL — `FileNotFoundError: .../infra/grafana/dashboards/latency.json`.

- [ ] **Step 3: Create the dashboard JSON**

Create `infra/grafana/dashboards/latency.json`:

```json
{
  "id": null,
  "uid": "usan-latency",
  "title": "USAN · Latency",
  "tags": ["usan", "latency"],
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "",
  "refresh": "1m",
  "time": { "from": "now-24h", "to": "now" },
  "templating": { "list": [] },
  "annotations": { "list": [] },
  "panels": [
    {
      "id": 1,
      "type": "timeseries",
      "title": "Response latency p50 / p95 / p99 (target 1200 ms)",
      "description": "User-perceived end-of-speech to first-audio gap (spec §7). Target: p95 <= 1200 ms.",
      "gridPos": { "h": 9, "w": 24, "x": 0, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, $__interval),\n  percentile_cont(0.5)  WITHIN GROUP (ORDER BY response_latency_ms) AS \"p50\",\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY response_latency_ms) AS \"p95\",\n  percentile_cont(0.99) WITHIN GROUP (ORDER BY response_latency_ms) AS \"p99\"\nFROM turn_metrics\nWHERE $__timeFilter(created_at) AND response_latency_ms IS NOT NULL\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "ms",
          "custom": {
            "drawStyle": "line",
            "lineWidth": 1,
            "fillOpacity": 10,
            "showPoints": "never",
            "thresholdsStyle": { "mode": "line" }
          },
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "value": null, "color": "green" },
              { "value": 1200, "color": "red" }
            ]
          }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 2,
      "type": "timeseries",
      "title": "Per-stage p95 (STT duration / LLM ttft / TTS ttfb)",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 9 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, $__interval),\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY stt_duration_ms) AS \"STT p95\",\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ttft_ms)    AS \"LLM ttft p95\",\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY tts_ttfb_ms)    AS \"TTS ttfb p95\"\nFROM turn_metrics\nWHERE $__timeFilter(created_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "ms",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 3,
      "type": "histogram",
      "title": "Per-turn response latency distribution",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 9 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT response_latency_ms\nFROM turn_metrics\nWHERE $__timeFilter(created_at) AND response_latency_ms IS NOT NULL"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "ms" }, "overrides": [] },
      "options": { "bucketSize": 100, "legend": { "displayMode": "list", "placement": "bottom", "showLegend": false } }
    },
    {
      "id": 4,
      "type": "table",
      "title": "Worst calls by response latency (top 20)",
      "description": "Drill target: copy call_id into the call record. No PHI (id only).",
      "gridPos": { "h": 9, "w": 24, "x": 0, "y": 17 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT tm.call_id::text AS call_id,\n  MAX(tm.response_latency_ms) AS worst_turn_ms,\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY tm.response_latency_ms) AS p95_ms,\n  COUNT(*) AS turns\nFROM turn_metrics tm\nWHERE $__timeFilter(tm.created_at) AND tm.response_latency_ms IS NOT NULL\nGROUP BY tm.call_id\nORDER BY worst_turn_ms DESC NULLS LAST\nLIMIT 20"
        }
      ],
      "fieldConfig": {
        "defaults": { "custom": { "align": "auto" } },
        "overrides": [
          { "matcher": { "id": "byName", "options": "worst_turn_ms" }, "properties": [ { "id": "unit", "value": "ms" } ] },
          { "matcher": { "id": "byName", "options": "p95_ms" }, "properties": [ { "id": "unit", "value": "ms" } ] }
        ]
      },
      "options": { "showHeader": true }
    }
  ]
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest scripts/tests/test_latency_dashboard.py -v`
Expected: PASS (all 4 tests). Also run the full scripts suite: `python -m pytest scripts/tests -v` — green.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana/dashboards/latency.json scripts/tests/test_latency_dashboard.py
git commit -m "feat(infra): add Grafana Latency dashboard (turn_metrics percentiles)"
```

---

## Task 3: Cost dashboard (`cost.json`)

**Files:**
- Create: `infra/grafana/dashboards/cost.json`
- Test: `scripts/tests/test_cost_dashboard.py`

Source: Postgres `call_metrics` (+ `calls`/`elders` for per-elder). Panels: cost/call avg+p95; blended $/min vs RetellAI baseline (constant var); daily spend stacked by component; cost per elder (top 20); month-to-date spend; projected monthly spend.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_cost_dashboard.py`:

```python
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
    for col in ("cost_total_usd", "cost_telephony_usd", "cost_llm_usd", "cost_tts_usd", "cost_stt_usd"):
        assert col in sql
    assert "${retell_baseline}" in sql
    # per-elder keyed on external_id, never name (PHI).
    assert "external_id" in sql
    assert "e.name" not in sql and "elders.name" not in sql


def test_cost_no_overlap():
    doc = load_dashboard("cost.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest scripts/tests/test_cost_dashboard.py -v`
Expected: FAIL — `FileNotFoundError: .../cost.json`.

- [ ] **Step 3: Create the dashboard JSON**

Create `infra/grafana/dashboards/cost.json`:

```json
{
  "id": null,
  "uid": "usan-cost",
  "title": "USAN · Cost",
  "tags": ["usan", "cost"],
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "",
  "refresh": "5m",
  "time": { "from": "now-30d", "to": "now" },
  "annotations": { "list": [] },
  "templating": {
    "list": [
      {
        "name": "retell_baseline",
        "label": "RetellAI $/min baseline",
        "type": "constant",
        "query": "0.10",
        "current": { "text": "0.10", "value": "0.10" },
        "hide": 2
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "timeseries",
      "title": "Cost per call (avg, p95)",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, $__interval),\n  AVG(cost_total_usd) AS \"avg\",\n  percentile_cont(0.95) WITHIN GROUP (ORDER BY cost_total_usd) AS \"p95\"\nFROM call_metrics\nWHERE $__timeFilter(created_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "currencyUSD",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 2,
      "type": "timeseries",
      "title": "Blended $/min vs RetellAI baseline",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, '1d'),\n  SUM(cost_total_usd) / NULLIF(SUM(duration_seconds), 0) * 60 AS \"blended $/min\",\n  ${retell_baseline}::numeric AS \"RetellAI baseline\"\nFROM call_metrics\nWHERE $__timeFilter(created_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "currencyUSD",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 0, "showPoints": "auto" }
        },
        "overrides": [
          {
            "matcher": { "id": "byName", "options": "RetellAI baseline" },
            "properties": [
              { "id": "custom.lineStyle", "value": { "fill": "dash", "dash": [10, 10] } },
              { "id": "color", "value": { "mode": "fixed", "fixedColor": "red" } }
            ]
          }
        ]
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "none" }
      }
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "Daily spend by component",
      "gridPos": { "h": 9, "w": 24, "x": 0, "y": 8 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, '1d'),\n  SUM(cost_telephony_usd) AS \"Telephony\",\n  SUM(cost_llm_usd)       AS \"LLM\",\n  SUM(cost_stt_usd)       AS \"STT\",\n  SUM(cost_tts_usd)       AS \"TTS\",\n  SUM(cost_storage_usd)   AS \"Storage\"\nFROM call_metrics\nWHERE $__timeFilter(created_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "currencyUSD",
          "custom": {
            "drawStyle": "bars",
            "fillOpacity": 80,
            "lineWidth": 0,
            "stacking": { "mode": "normal", "group": "A" }
          }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 4,
      "type": "table",
      "title": "Cost per elder (top 20)",
      "description": "Keyed on external_id, not name (PHI). Behind operator-CIDR + auth.",
      "gridPos": { "h": 9, "w": 12, "x": 0, "y": 17 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT COALESCE(e.external_id, left(e.id::text, 8)) AS elder,\n  COUNT(cm.call_id) AS calls,\n  SUM(cm.cost_total_usd) AS total_usd,\n  AVG(cm.cost_total_usd) AS avg_per_call_usd\nFROM call_metrics cm\nJOIN calls c  ON c.id = cm.call_id\nJOIN elders e ON e.id = c.elder_id\nWHERE $__timeFilter(cm.created_at)\nGROUP BY 1\nORDER BY total_usd DESC NULLS LAST\nLIMIT 20"
        }
      ],
      "fieldConfig": {
        "defaults": { "custom": { "align": "auto" } },
        "overrides": [
          { "matcher": { "id": "byName", "options": "total_usd" }, "properties": [ { "id": "unit", "value": "currencyUSD" } ] },
          { "matcher": { "id": "byName", "options": "avg_per_call_usd" }, "properties": [ { "id": "unit", "value": "currencyUSD" } ] }
        ]
      },
      "options": { "showHeader": true }
    },
    {
      "id": 5,
      "type": "stat",
      "title": "Month-to-date spend",
      "gridPos": { "h": 9, "w": 6, "x": 12, "y": 17 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT COALESCE(SUM(cost_total_usd), 0) AS mtd_usd\nFROM call_metrics\nWHERE created_at >= date_trunc('month', now())"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "currencyUSD" }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "none"
      }
    },
    {
      "id": 6,
      "type": "stat",
      "title": "Projected monthly spend",
      "description": "MTD spend linearly extrapolated to month end. Ignores the dashboard time range by design.",
      "gridPos": { "h": 9, "w": 6, "x": 18, "y": 17 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT (COALESCE(SUM(cost_total_usd), 0) / GREATEST(EXTRACT(DAY FROM now()), 1))\n  * EXTRACT(DAY FROM (date_trunc('month', now()) + interval '1 month' - interval '1 day')) AS projected_usd\nFROM call_metrics\nWHERE created_at >= date_trunc('month', now())"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "currencyUSD" }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "none"
      }
    }
  ]
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest scripts/tests/test_cost_dashboard.py -v`
Expected: PASS (all 5 tests). Full suite `python -m pytest scripts/tests -v` — green.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana/dashboards/cost.json scripts/tests/test_cost_dashboard.py
git commit -m "feat(infra): add Grafana Cost dashboard (call_metrics spend model)"
```

---

## Task 4: Business/Care dashboard (`business.json`)

**Files:**
- Create: `infra/grafana/dashboards/business.json`
- Test: `scripts/tests/test_business_dashboard.py`

Source: Postgres `calls` / `wellness_logs` / `medication_logs`. Panels: call volume in/out; outcome breakdown; success rate; retry effectiveness by attempt; avg duration; mood & pain trends; medication adherence %.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_business_dashboard.py`:

```python
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
    # never select PHI free-text / names
    assert "notes" not in sql and "e.name" not in sql


def test_business_no_overlap():
    doc = load_dashboard("business.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest scripts/tests/test_business_dashboard.py -v`
Expected: FAIL — `FileNotFoundError: .../business.json`.

- [ ] **Step 3: Create the dashboard JSON**

Create `infra/grafana/dashboards/business.json`:

```json
{
  "id": null,
  "uid": "usan-business",
  "title": "USAN · Business / Care",
  "tags": ["usan", "business"],
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "",
  "refresh": "5m",
  "time": { "from": "now-30d", "to": "now" },
  "templating": { "list": [] },
  "annotations": { "list": [] },
  "panels": [
    {
      "id": 1,
      "type": "timeseries",
      "title": "Call volume (inbound / outbound)",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, '1d'),\n  COUNT(*) FILTER (WHERE direction = 'outbound') AS \"Outbound\",\n  COUNT(*) FILTER (WHERE direction = 'inbound')  AS \"Inbound\"\nFROM calls\nWHERE $__timeFilter(created_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "custom": { "drawStyle": "bars", "fillOpacity": 70, "lineWidth": 0, "stacking": { "mode": "normal", "group": "A" } }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 2,
      "type": "piechart",
      "title": "Call outcomes",
      "gridPos": { "h": 8, "w": 6, "x": 12, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT status, COUNT(*) AS n\nFROM calls\nWHERE $__timeFilter(created_at)\nGROUP BY status\nORDER BY n DESC"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "short" }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": true },
        "pieType": "donut",
        "legend": { "displayMode": "list", "placement": "right", "showLegend": true }
      }
    },
    {
      "id": 3,
      "type": "stat",
      "title": "Success rate (completed)",
      "gridPos": { "h": 8, "w": 6, "x": 18, "y": 0 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT 100.0 * COUNT(*) FILTER (WHERE status = 'completed') / NULLIF(COUNT(*), 0) AS success_pct\nFROM calls\nWHERE $__timeFilter(created_at)"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "percent",
          "thresholds": { "mode": "absolute", "steps": [ { "value": null, "color": "red" }, { "value": 60, "color": "orange" }, { "value": 80, "color": "green" } ] }
        },
        "overrides": []
      },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "orientation": "auto", "textMode": "auto", "colorMode": "background", "graphMode": "none"
      }
    },
    {
      "id": 4,
      "type": "barchart",
      "title": "Retry effectiveness by attempt",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "table",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT attempt::text AS attempt,\n  COUNT(*) AS calls,\n  ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'completed') / NULLIF(COUNT(*), 0), 1) AS completed_pct\nFROM calls\nWHERE $__timeFilter(created_at)\nGROUP BY attempt\nORDER BY attempt"
        }
      ],
      "fieldConfig": {
        "defaults": { "unit": "short" },
        "overrides": [ { "matcher": { "id": "byName", "options": "completed_pct" }, "properties": [ { "id": "unit", "value": "percent" } ] } ]
      },
      "options": { "orientation": "auto", "xField": "attempt", "showValue": "auto", "stacking": "none", "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true } }
    },
    {
      "id": 5,
      "type": "timeseries",
      "title": "Average call duration",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(created_at, '1d'),\n  AVG(duration_seconds) AS \"avg duration\"\nFROM calls\nWHERE $__timeFilter(created_at) AND duration_seconds IS NOT NULL\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "single", "sort": "none" }
      }
    },
    {
      "id": 6,
      "type": "timeseries",
      "title": "Mood & pain trends (daily avg)",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 16 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(logged_at, '1d'),\n  AVG(mood)       AS \"Avg mood\",\n  AVG(pain_level) AS \"Avg pain\"\nFROM wellness_logs\nWHERE $__timeFilter(logged_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "auto" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "none" }
      }
    },
    {
      "id": 7,
      "type": "timeseries",
      "title": "Medication adherence %",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 16 },
      "datasource": { "type": "postgres", "uid": "postgres-ro" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "postgres", "uid": "postgres-ro" },
          "format": "time_series",
          "rawQuery": true,
          "editorMode": "code",
          "rawSql": "SELECT $__timeGroupAlias(logged_at, '1d'),\n  100.0 * COUNT(*) FILTER (WHERE taken) / NULLIF(COUNT(*), 0) AS \"Adherence %\"\nFROM medication_logs\nWHERE $__timeFilter(logged_at)\nGROUP BY 1 ORDER BY 1"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "percent", "min": 0, "max": 100,
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "auto" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "single", "sort": "none" }
      }
    }
  ]
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest scripts/tests/test_business_dashboard.py -v`
Expected: PASS (all 4 tests). Full suite `python -m pytest scripts/tests -v` — green.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana/dashboards/business.json scripts/tests/test_business_dashboard.py
git commit -m "feat(infra): add Grafana Business/Care dashboard (calls, wellness, meds)"
```

---

## Task 5: System / RED dashboard (`system.json`)

**Files:**
- Create: `infra/grafana/dashboards/system.json`
- Test: `scripts/tests/test_system_dashboard.py`

Source: Prometheus (the `usan-api` scrape job). Panels: API request rate by handler; API latency p95; 5xx rate; webhook deliveries; tool-call outcomes; service up; calls completed; a `text` panel noting host metrics live in Cloud Monitoring (deviation #1).

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_system_dashboard.py`:

```python
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
    blob = " ".join(p.get("options", {}).get("content", "") for p in text_panels).lower()
    assert "cloud monitoring" in blob


def test_system_no_overlap():
    doc = load_dashboard("system.json")
    assert gridpos_overlaps(list(iter_panels(doc))) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest scripts/tests/test_system_dashboard.py -v`
Expected: FAIL — `FileNotFoundError: .../system.json`.

- [ ] **Step 3: Create the dashboard JSON**

Create `infra/grafana/dashboards/system.json`:

```json
{
  "id": null,
  "uid": "usan-system",
  "title": "USAN · System (RED)",
  "tags": ["usan", "system", "red"],
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "",
  "refresh": "30s",
  "time": { "from": "now-6h", "to": "now" },
  "templating": { "list": [] },
  "annotations": { "list": [] },
  "panels": [
    {
      "id": 1,
      "type": "timeseries",
      "title": "API request rate by handler",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum by (handler) (rate(http_requests_total[$__rate_interval]))",
          "legendFormat": "{{handler}}",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "reqps",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 2,
      "type": "timeseries",
      "title": "API latency p95 (per handler)",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "histogram_quantile(0.95, sum by (handler, le) (rate(http_request_duration_seconds_bucket[$__rate_interval])))",
          "legendFormat": "{{handler}}",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "API 5xx rate",
      "gridPos": { "h": 8, "w": 8, "x": 0, "y": 8 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum(rate(http_requests_total{status=\"5xx\"}[$__rate_interval]))",
          "legendFormat": "5xx/s",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "reqps",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 20, "showPoints": "auto" },
          "thresholds": { "mode": "absolute", "steps": [ { "value": null, "color": "green" }, { "value": 0.1, "color": "red" } ] }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": false },
        "tooltip": { "mode": "single", "sort": "none" }
      }
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "Webhook deliveries (type / outcome)",
      "gridPos": { "h": 8, "w": 8, "x": 8, "y": 8 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum by (type, outcome) (rate(usan_webhooks_total[$__rate_interval]))",
          "legendFormat": "{{type}} · {{outcome}}",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "reqps",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 5,
      "type": "timeseries",
      "title": "Tool calls (tool / outcome)",
      "gridPos": { "h": 8, "w": 8, "x": 16, "y": 8 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum by (tool, outcome) (rate(usan_tool_calls_total[$__rate_interval]))",
          "legendFormat": "{{tool}} · {{outcome}}",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "reqps",
          "custom": { "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    },
    {
      "id": 6,
      "type": "stat",
      "title": "Service up (API)",
      "gridPos": { "h": 6, "w": 6, "x": 0, "y": 16 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "up{job=\"usan-api\"}",
          "legendFormat": "api",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "mappings": [
            { "type": "value", "options": { "0": { "text": "DOWN", "color": "red" }, "1": { "text": "UP", "color": "green" } } }
          ],
          "thresholds": { "mode": "absolute", "steps": [ { "value": null, "color": "red" }, { "value": 1, "color": "green" } ] }
        },
        "overrides": []
      },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "orientation": "auto", "textMode": "auto", "colorMode": "background", "graphMode": "none"
      }
    },
    {
      "id": 7,
      "type": "stat",
      "title": "Calls completed (range)",
      "gridPos": { "h": 6, "w": 6, "x": 6, "y": 16 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum(increase(usan_calls_total[$__range]))",
          "legendFormat": "completed",
          "range": true
        }
      ],
      "fieldConfig": { "defaults": { "unit": "short" }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "area"
      }
    },
    {
      "id": 8,
      "type": "text",
      "title": "Host metrics",
      "gridPos": { "h": 6, "w": 12, "x": 12, "y": 16 },
      "options": {
        "mode": "markdown",
        "content": "**Host CPU / memory / disk** are not in Prometheus.\n\nThey live in **GCP Cloud Monitoring** (the VM Ops Agent writes them). No Grafana Cloud Monitoring datasource is provisioned yet — view host metrics in the [Cloud Monitoring console](https://console.cloud.google.com/monitoring) for project `usan-retirement`, or wire the datasource + `roles/monitoring.viewer` in a follow-up (spec §9, deviation #1)."
      }
    }
  ]
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest scripts/tests/test_system_dashboard.py -v`
Expected: PASS (all 5 tests). Full suite `python -m pytest scripts/tests -v` — green.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana/dashboards/system.json scripts/tests/test_system_dashboard.py
git commit -m "feat(infra): add Grafana System/RED dashboard (Prometheus)"
```

---

## Task 6: Collection invariants + deploy runbook

**Files:**
- Create: `scripts/tests/test_dashboards_collection.py`
- Modify: `infra/README.md`

Locks the cross-file invariants (all four present, uids and titles globally unique, every file validates) and documents how the dashboards reach the VM and how to verify them.

- [ ] **Step 1: Write the failing collection test**

Create `scripts/tests/test_dashboards_collection.py`:

```python
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
    assert on_disk == set(EXPECTED), f"unexpected dashboard files: {on_disk ^ set(EXPECTED)}"
```

- [ ] **Step 2: Run the collection test to verify it passes**

By now all four dashboards exist, so this should pass immediately.

Run: `python -m pytest scripts/tests/test_dashboards_collection.py -v`
Expected: PASS (3 tests). If `test_no_unexpected_json_files...` fails, an extra/misnamed JSON is in the dir — remove it.

- [ ] **Step 3: Add the MON-3 runbook section to `infra/README.md`**

Append this section to `infra/README.md` (after the existing "Monitoring (Grafana + Prometheus) — MON-2" section):

```markdown
### Dashboards (MON-3)

The four Grafana dashboards are checked-in JSON under `infra/grafana/dashboards/`
(`latency.json`, `cost.json`, `business.json`, `system.json`), loaded by the
file provider MON-2 installed (folder **USAN**, container path
`/var/lib/grafana/dashboards`, 30 s rescan, `allowUiUpdates=false` so the repo is
the source of truth).

**They ship automatically.** `build.yml` already scp's the whole `infra/grafana`
tree to the VM, so a `v*` tag deploy copies the new JSON; Grafana picks it up
within 30 s. No compose, datasource, or workflow change is needed.

**Datasources they bind to** (provisioned by MON-2, referenced by uid):
- `postgres-ro` — Latency, Cost, Business/Care (read-only `grafana_ro` role).
- `prometheus` — System/RED.

**Host CPU/mem/disk are not here** — no Cloud Monitoring datasource is
provisioned. View host metrics in the GCP Cloud Monitoring console, or wire the
Google Cloud Monitoring datasource + `roles/monitoring.viewer` in a follow-up.

**Verify after deploy** (from an operator IP inside `GRAFANA_ALLOWED_CIDR`):
1. Browse `https://grafana.<domain>/dashboards` → folder **USAN** lists all four.
2. Open **USAN · System (RED)** → "Service up (API)" shows UP and request-rate
   panels populate (Prometheus path healthy).
3. Open **USAN · Latency** → panels render without a datasource error (confirms
   the `grafana_ro` Postgres path + `turn_metrics` access).
4. If a panel shows "datasource not found", the dashboard JSON references a uid
   other than `prometheus` / `postgres-ro` — fix the JSON, not Grafana.

**Edit/add a dashboard:** change the JSON under `infra/grafana/dashboards/`,
keep `id: null` and a unique `uid`/`title`, run `python -m pytest scripts/tests`,
commit, and ship on the next tag. CI's `pytest (scripts)` job validates structure
(datasource uids, gridPos, no PHI columns) on every PR.
```

- [ ] **Step 4: Run the full scripts suite**

Run: `python -m pytest scripts/tests -v`
Expected: PASS — the contract unit tests, all four per-dashboard suites, and the collection invariants are green.

- [ ] **Step 5: Commit**

```bash
git add scripts/tests/test_dashboards_collection.py infra/README.md
git commit -m "feat(infra): MON-3 dashboard collection invariants + deploy runbook"
```

---

## Self-Review

**1. Spec coverage (§9 dashboard catalog):**

| Spec requirement | Task | Panel / query |
|---|---|---|
| Latency: end-of-turn p50/p95/p99 + 1200 ms target | Task 2 | panel 1, `percentile_cont` over `response_latency_ms`, threshold step 1200 |
| Latency: per-stage STT/LLM ttft/TTS ttfb percentiles | Task 2 | panel 2 |
| Latency: worst-calls table (drill to call_id) | Task 2 | panel 4 |
| Latency: per-turn distribution | Task 2 | panel 3 (histogram) |
| Cost: cost/call (avg, p95) | Task 3 | panel 1 |
| Cost: daily/monthly spend stacked by component | Task 3 | panel 3 (stacked bars) |
| Cost: blended $/min vs RetellAI baseline | Task 3 | panel 2 + `retell_baseline` constant var |
| Cost: cost per elder (top N) | Task 3 | panel 4 (keyed external_id) |
| Cost: projected monthly spend | Task 3 | panel 6 (+ panel 5 MTD) |
| Business: call volume in/out | Task 4 | panel 1 |
| Business: success/no-answer/voicemail/failed/DNC rates | Task 4 | panel 2 (outcomes pie) + panel 3 (success stat) |
| Business: retry effectiveness by attempt | Task 4 | panel 4 |
| Business: avg duration | Task 4 | panel 5 |
| Business: mood & pain trends | Task 4 | panel 6 |
| Business: medication adherence % | Task 4 | panel 7 |
| System: API req rate / latency / 5xx | Task 5 | panels 1, 2, 3 |
| System: webhook + tool-endpoint latency & errors | Task 5 | panels 4, 5 |
| System: host CPU/mem/disk | Task 5 | **deviation #1** — text panel 8 (no Cloud Monitoring DS) |
| System: service up | Task 5 | panel 6 (`up{job="usan-api"}`) |
| §9 provisioned-as-code (4 files, USAN folder) | Tasks 2–5 | files in `infra/grafana/dashboards/` |
| §11 "validate dashboard JSON (schema/lint)" | Task 1 + per-dashboard tests | `validate_dashboard` in `pytest (scripts)` |

**2. Placeholder scan:** No "TBD"/"implement later". Every dashboard JSON is complete and copy-pasteable; every test shows full code; every command has expected output.

**3. Type/name consistency:** Datasource uids (`postgres-ro`, `prometheus`), the validator API (`validate_dashboard`, `iter_panels`, `gridpos_overlaps`, `load_dashboard`, `DASHBOARDS_DIR`, `EXPECTED`, `ALLOWED_DS_UIDS`), filenames, and uids (`usan-latency`/`usan-cost`/`usan-business`/`usan-system`) are identical across Tasks 1–6. Column and metric names match the verified-facts section verbatim. `gridPos` values were laid out non-overlapping per dashboard and are asserted by both the validator and each dashboard test.

**Gaps:** None against the in-scope spec sections. The only spec item not implemented is host CPU/mem/disk (deviation #1), which is blocked on infra MON-2 chose not to provision and is explicitly documented in the System dashboard and runbook.
