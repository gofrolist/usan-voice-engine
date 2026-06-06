# Plan MON-2: Prometheus + Grafana Observability Platform

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the self-hosted observability platform on the VM — a Prometheus `/metrics` endpoint on the API plus a Prometheus + Grafana stack (with a least-privilege `grafana_ro` DB role, behind a Caddy operator-CIDR-gated TLS subdomain) — so MON-3 can drop in dashboards-as-code.

**Architecture:** The API exposes RED metrics + three business counters at `/metrics` via `prometheus-fastapi-instrumentator`; that endpoint is internal-only (scraped over the compose bridge, blocked at Caddy's edge). A `prometheus` container scrapes `api:8000/metrics`; a `grafana` container (provisioned-as-code with Prometheus + read-only Postgres datasources) is reverse-proxied by Caddy at `grafana.<domain>`, gated by an operator-CIDR allowlist at L7. Grafana reads Postgres through a dedicated `grafana_ro` role with SELECT on the six non-`transcripts` reporting tables. Terraform adds the DNS record, the `grafana_ro` Cloud SQL user, and generated admin/RO passwords.

**Tech Stack:** FastAPI (Python 3.14, uv) · `prometheus-fastapi-instrumentator` 8.x · `prom/prometheus:v3.12.0` · `grafana/grafana:12.4.4` · Caddy v2 · Alembic (raw `op.execute`) · Docker Compose overlays · Terraform (GCP Cloud SQL + Cloudflare DNS).

**Source spec:** `docs/superpowers/specs/2026-06-05-monitoring-dashboard-design.md` (§8 Prometheus, §9 Grafana, §10 deploy/access, §11 testing, §12 phasing). MON-2 = spec **phases 3 + 4**. MON-1 (phases 1–2: Postgres metrics pipeline) shipped in PR #39. MON-3 (phase 5: the four dashboards) is a separate plan.

---

## Scope & boundaries

**In scope (this plan):**
1. API `/metrics` endpoint (instrumentator built-in RED metrics).
2. Three custom counters: `usan_calls_total{direction,end_reason}`, `usan_webhooks_total{type,outcome}`, `usan_tool_calls_total{tool,outcome}`.
3. Caddy `/metrics` 403 rule (keep the scrape endpoint off the public internet).
4. `prometheus` + `grafana` containers (prod compose overlay) with named volumes.
5. Grafana provisioning-as-code skeleton: Prometheus + read-only Postgres datasources + a dashboard *provider* pointing at an (empty for now) dashboards dir.
6. `grafana_ro` least-privilege Postgres role (Alembic migration `0009` for the GRANTs; Terraform `google_sql_user` for the login password).
7. Caddy `grafana.<domain>` subdomain, operator-CIDR-gated at L7.
8. Terraform: `grafana` DNS A-record, `grafana_ro` Cloud SQL user, generated Grafana admin + RO passwords (sensitive outputs), optional `roles/monitoring.viewer` IAM.
9. Deploy wiring: ship the new overlay + config dirs to the VM and add them to the prod `docker compose up`.

**Out of scope (MON-3 or later, do NOT build here):**
- The four Grafana dashboard JSON files (`latency.json`, `cost.json`, `business.json`, `system.json`).
- The Cloud Monitoring **datasource** wiring (host CPU/mem/disk panels). The `roles/monitoring.viewer` IAM is added here as prep, but the datasource itself is MON-3.
- Routing cost/latency through Prometheus. **Per spec §3/§6/§7 those live in Postgres**; only the three `usan_*_total` counters and the instrumentator's RED metrics go to Prometheus. Do not add cost/latency histograms to `/metrics`.

---

## Reconciliations / deliberate deviations from the spec (read before implementing)

These are intentional, reasoned departures grounded in the live codebase. A spec reviewer should expect them.

1. **`end_reason` label value = bounded `call.status` enum, not the free-text `calls.end_reason`.** `routers/tools.py::end_call` already documents that `body.reason` is LLM free-text that "could carry clinical content," and the spec (§6, §15) mandates the Prometheus plane carry **no PHI** and low cardinality. Free-text would violate both. We use `call.status.value` (a bounded enum: `completed`, `voicemail_left`, `no_answer`, …).

2. **`usan_calls_total` increments only at the agent-side `end_call` chokepoint** (answered/completed calls). Other terminal outcomes (no-answer/voicemail/failed dial, retry-cancel) reach terminal status on code paths that do not flow through the API. Those outcomes are authoritative in Postgres `calls.status`/`end_reason` and surface on the MON-3 Business dashboard via SQL (spec §3, §9). The Prometheus counter is a live-RED convenience, **not** the business source of truth. Comprehensive call-outcome attribution is a deliberate non-goal for MON-2.

3. **`GRAFANA_ALLOWED_CIDR` is an `.env` key, not a Terraform variable.** Spec §10 lists "Terraform var `grafana_allowed_cidr`," but §10 also says the allowlist is "**enforced at L7 in Caddy, not via a VM firewall rule**" (443 is shared across api/lk/grafana by SNI). Since no firewall rule consumes it, the CIDR lives in the `usan-prod-env` `.env` blob like every other Caddy config value (`API_DOMAIN`, `LIVEKIT_DOMAIN`). No `.tf` variable is created for it.

4. **`grafana_ro` is NOT granted SELECT on `transcripts`.** Spec §9 lists six tables (`calls`, `elders`, `wellness_logs`, `medication_logs`, `turn_metrics`, `call_metrics`) and omits `transcripts`. We grant exactly those six and explicitly exclude `transcripts` (raw conversation PHI, the most sensitive table, unneeded by any planned dashboard). We use per-table grants with **no** `ALTER DEFAULT PRIVILEGES`, so future tables are excluded by default.

5. **Grafana secrets ride the existing single `usan-prod-env` `.env` blob** (delivered by `startup.sh`), injected as container env vars — matching how every other secret in this repo is handled. We do **not** introduce Docker file-secrets or per-key Secret Manager secrets (a new pattern this repo doesn't use). The passwords are *generated* by Terraform `random_password` and surfaced as sensitive outputs (mirroring `random_password.db`), then folded into the `.env` blob by the operator exactly like `db_password`.

---

## Critical implementation gotchas (the landmines)

- **`prometheus_client` auto-appends `_total` to Counter sample names.** To expose `usan_calls_total` you must construct `Counter("usan_calls", …)` — naming it `Counter("usan_calls_total", …)` yields the sample `usan_calls_total_total`. Same for `usan_webhooks` and `usan_tool_calls`.
- **One global registry + a function-scoped `client` fixture** that rebuilds the app per test ⇒ naïvely instrumenting inside `create_app()` raises `ValueError: Duplicated timeseries in CollectorRegistry` on the 2nd app build. Mitigation (Task 2): a **process-wide singleton `Instrumentator`** (built-in metrics created once) + **module-scope** custom counters (imported once). Counters never reset within a process, so tests assert **before/after deltas**, never absolute values.
- **`uv.lock` must be regenerated** (`uv lock`) after editing `pyproject.toml`, or CI's `uv sync --frozen` (lint.yml:19, test.yml:19) fails.
- **CI runs `uv run mypy`** (lint.yml; strict mode, `files = ["src"]`) in addition to ruff. Run `uv run mypy` + `uv run ruff format --check .` locally before every commit. `ignore_missing_imports = true` covers the instrumentator if it ships no stubs.
- **The `@track_tool` decorator wraps the handler body, not its dependencies.** A request that fails in `require_service_token` (e.g. no token) never enters the handler, so the counter doesn't move. To test the `outcome="error"` path deterministically, send a *valid* service JWT for a *nonexistent* call → `_authorize_call` raises `HTTPException(404)` **inside** the handler → the wrapper records `error` and re-raises.

---

## File Structure

| File | New/Modify | Responsibility |
|---|---|---|
| `apps/api/pyproject.toml` | Modify | Add `prometheus-fastapi-instrumentator` + `prometheus-client` deps |
| `apps/api/uv.lock` | Modify (regen) | Lock the new deps (`uv lock`) |
| `apps/api/src/usan_api/observability/__init__.py` | Create | Package marker |
| `apps/api/src/usan_api/observability/custom_metrics.py` | Create | The 3 Counters (default registry) + `track_tool` decorator |
| `apps/api/src/usan_api/observability/instrumentation.py` | Create | Singleton `Instrumentator` + `setup_metrics(app)` |
| `apps/api/src/usan_api/main.py` | Modify | Call `setup_metrics(app)` in `create_app()` |
| `apps/api/src/usan_api/routers/webhooks.py` | Modify | Increment `usan_webhooks_total` |
| `apps/api/src/usan_api/routers/tools.py` | Modify | `@track_tool(...)` on each handler + `usan_calls_total` in `end_call` |
| `apps/api/tests/test_observability.py` | Create | `/metrics` exposure, no-duplicate, not-rate-limited, counter increments |
| `apps/api/migrations/versions/0009_grafana_ro_role.py` | Create | `grafana_ro` role + per-table SELECT grants |
| `apps/api/tests/test_grafana_ro_role.py` | Create | Role exists; SELECT on the 6 tables; denied on `transcripts` |
| `infra/Caddyfile` | Modify | `/metrics` 403 on API host; new `grafana.<domain>` block |
| `infra/prometheus/prometheus.yml` | Create | Scrape `api:8000/metrics`, 30d retention |
| `infra/grafana/provisioning/datasources/datasources.yml` | Create | Prometheus + read-only Postgres datasources |
| `infra/grafana/provisioning/dashboards/dashboards.yml` | Create | Dashboard provider (file) → `/var/lib/grafana/dashboards` |
| `infra/grafana/dashboards/.gitkeep` | Create | Empty dir MON-3 fills |
| `infra/docker-compose.monitoring.yml` | Create | `prometheus` + `grafana` services + volumes (prod overlay) |
| `infra/docker-compose.tls.yml` | Modify | Add `GRAFANA_DOMAIN` + `GRAFANA_ALLOWED_CIDR` to caddy env |
| `infra/.env.prod.example` | Modify | Document all new Grafana `.env` keys |
| `infra/terraform/dns.tf` | Modify | `cloudflare_dns_record.grafana` |
| `infra/terraform/database.tf` | Modify | `random_password.grafana_ro` + `google_sql_user.grafana_ro` |
| `infra/terraform/main.tf` | Modify | `random_password.grafana_admin` + optional `roles/monitoring.viewer` |
| `infra/terraform/outputs.tf` | Modify | Sensitive outputs for the two new passwords |
| `.github/workflows/build.yml` | Modify | scp the new files + add `-f docker-compose.monitoring.yml` |
| `infra/README.md` | Modify | Grafana deploy/runbook section |

---

# PHASE 3 — API Prometheus (spec §8)

### Task 1: Add Prometheus dependencies

**Files:**
- Modify: `apps/api/pyproject.toml` (the `[project].dependencies` list)
- Modify: `apps/api/uv.lock` (regenerated, not hand-edited)

- [ ] **Step 1: Add the two dependencies**

In `apps/api/pyproject.toml`, append to the `dependencies` array (after `"limits>=5.8.0",`):

```toml
    "limits>=5.8.0",
    "prometheus-fastapi-instrumentator>=8.0.0,<9",
    "prometheus-client>=0.21.1,<0.22",
```

- [ ] **Step 2: Regenerate the lockfile and sync**

Run (from `apps/api`):
```bash
cd apps/api && uv lock && uv sync
```
Expected: `uv.lock` updates to include `prometheus-fastapi-instrumentator` and `prometheus-client`; `uv sync` installs them with no resolution error.

- [ ] **Step 3: Verify the import works under Python 3.14**

Run:
```bash
cd apps/api && uv run python -c "import prometheus_fastapi_instrumentator, prometheus_client; print('ok')"
```
Expected: prints `ok` (confirms the pins resolve and import on 3.14).

- [ ] **Step 4: Commit**

```bash
git add apps/api/pyproject.toml apps/api/uv.lock
git commit -m "build(api): add prometheus-fastapi-instrumentator + prometheus-client"
```

---

### Task 2: Custom metrics module + instrumentation wiring

**Files:**
- Create: `apps/api/src/usan_api/observability/__init__.py`
- Create: `apps/api/src/usan_api/observability/custom_metrics.py`
- Create: `apps/api/src/usan_api/observability/instrumentation.py`
- Modify: `apps/api/src/usan_api/main.py`
- Create: `apps/api/tests/test_observability.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_observability.py`:

```python
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from prometheus_client import REGISTRY

from usan_api.main import create_app


def _counter(name: str, labels: dict[str, str]) -> float:
    """Current value of a labeled counter sample (0.0 if not yet observed)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _set_min_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum env for create_app() to build (no DB connection needed)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/u")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_metrics_endpoint_exposes_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    # Custom families appear (HELP/TYPE lines) even before any increment.
    assert "usan_calls_total" in body
    assert "usan_webhooks_total" in body
    assert "usan_tool_calls_total" in body
    # Built-in RED family from the instrumentator.
    assert "http_request" in body


def test_create_app_twice_does_not_raise_duplicate_timeseries(monkeypatch):
    _set_min_env(monkeypatch)
    app1 = create_app()
    app2 = create_app()  # must NOT raise "Duplicated timeseries in CollectorRegistry"
    assert any(getattr(r, "path", None) == "/metrics" for r in app1.routes)
    assert any(getattr(r, "path", None) == "/metrics" for r in app2.routes)


def test_metrics_endpoint_is_not_rate_limited(client):
    # /metrics is not an operator route, so it must never 429 even when hammered.
    for _ in range(5):
        assert client.get("/metrics").status_code == 200


def _service_jwt(call_id, signing_key: str = "s" * 32) -> str:
    now = datetime.now(tz=timezone.utc)
    return jwt.encode(
        {"sub": "usan-agent", "call_id": str(call_id),
         "iat": now, "exp": now + timedelta(minutes=5)},
        signing_key, algorithm="HS256",
    )


def test_tool_call_error_path_increments_counter(client):
    # Valid JWT for a call that does not exist -> _authorize_call raises 404
    # INSIDE the handler -> @track_tool records outcome="error".
    cid = uuid4()
    labels = {"tool": "log_metrics", "outcome": "error"}
    before = _counter("usan_tool_calls_total", labels)
    r = client.post(
        "/v1/tools/log_metrics",
        json={
            "call_id": str(cid),
            "turns": [],
            "usage": {
                "llm_prompt_tokens": 0, "llm_completion_tokens": 0,
                "tts_characters": 0, "stt_audio_seconds": 0.0,
                "session_duration_seconds": 0.0,
            },
        },
        headers={"Authorization": f"Bearer {_service_jwt(cid)}"},
    )
    assert r.status_code == 404
    assert _counter("usan_tool_calls_total", labels) == before + 1


def test_invalid_webhook_increments_counter(client):
    labels = {"type": "unknown", "outcome": "invalid"}
    before = _counter("usan_webhooks_total", labels)
    r = client.post(
        "/webhooks/livekit", content=b"{}", headers={"Authorization": "bad"}
    )
    assert r.status_code == 401
    assert _counter("usan_webhooks_total", labels) == before + 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_observability.py -v`
Expected: FAIL — `/metrics` 404 (no endpoint), `ModuleNotFoundError: usan_api.observability`, and the counter helpers find nothing.

- [ ] **Step 3: Create the observability package marker**

Create `apps/api/src/usan_api/observability/__init__.py`:

```python
```
(empty file — package marker only)

- [ ] **Step 4: Create the custom metrics module**

Create `apps/api/src/usan_api/observability/custom_metrics.py`:

```python
"""Custom Prometheus metrics (spec §8) and a tool-call tracking decorator.

IMPORTANT: prometheus_client appends "_total" to a Counter's exposed sample
name. To expose `usan_calls_total` the Counter is constructed as `usan_calls`.

Metrics register against the process-global default registry at import time, so
they are created exactly once per process (module import is cached). Labels are
a small, bounded, PHI-FREE set — never put call_id, elder id, phone number, or
free-text reasons in a label (unbounded cardinality and a PHI leak; spec §6/§15).
"""

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from prometheus_client import Counter

P = ParamSpec("P")
R = TypeVar("R")

# direction: inbound|outbound ; end_reason: the bounded call_status enum value
# (completed, voicemail_left, no_answer, busy, failed, dnc_blocked, cancelled).
CALLS_TOTAL = Counter(
    "usan_calls",
    "Calls reaching a terminal state at the agent end_call hook.",
    labelnames=("direction", "end_reason"),
)

# type: the LiveKit event name (room_finished, egress_started, ...) or "unknown"
# when the signature failed before the event could be parsed.
# outcome: ok|invalid.
WEBHOOKS_TOTAL = Counter(
    "usan_webhooks",
    "Inbound webhook deliveries by event type and verification outcome.",
    labelnames=("type", "outcome"),
)

# tool: the tool endpoint name ; outcome: ok|error.
TOOL_CALLS_TOTAL = Counter(
    "usan_tool_calls",
    "Tool endpoint invocations by tool and outcome.",
    labelnames=("tool", "outcome"),
)


def track_tool(tool: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate a tool route handler to record usan_tool_calls_total{tool,outcome}.

    Wraps the handler body (not its dependencies): a request rejected by an auth
    dependency never reaches here, so only invocations that enter the handler are
    counted. functools.wraps preserves __wrapped__, so FastAPI still resolves the
    handler's real signature (Depends/Body params) through the wrapper.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = await func(*args, **kwargs)
            except Exception:
                TOOL_CALLS_TOTAL.labels(tool=tool, outcome="error").inc()
                raise
            TOOL_CALLS_TOTAL.labels(tool=tool, outcome="ok").inc()
            return result

        return wrapper

    return decorator
```

- [ ] **Step 5: Create the instrumentation module**

Create `apps/api/src/usan_api/observability/instrumentation.py`:

```python
"""Prometheus instrumentation wiring for the FastAPI app.

A single process-wide Instrumentator. prometheus_client metrics live in one
global registry per process, so the built-in RED collectors must be created
exactly once. Reusing this instance across create_app() calls (the test suite
rebuilds the app per test) adds the middleware + /metrics route to each app
WITHOUT re-registering collectors — which would raise
"Duplicated timeseries in CollectorRegistry".
"""

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

# Importing the custom metrics module here guarantees the three counters are
# registered on the default registry whenever /metrics is wired, so their
# HELP/TYPE lines appear even before the first increment.
from usan_api.observability import custom_metrics  # noqa: F401

_INSTRUMENTATOR = Instrumentator(
    should_group_status_codes=True,   # 2xx/4xx/5xx buckets -> low cardinality
    should_ignore_untemplated=True,   # drop unmatched paths (scanners)
    excluded_handlers=["/metrics", "/health"],  # don't instrument the scrape/health
)


def setup_metrics(app: FastAPI) -> None:
    """Instrument `app` and expose GET /metrics (internal scrape endpoint)."""
    _INSTRUMENTATOR.instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False, should_gzip=False
    )
```

- [ ] **Step 6: Wire it into the app factory**

In `apps/api/src/usan_api/main.py`, add the import (with the other `usan_api` imports near the top):

```python
from usan_api.observability.instrumentation import setup_metrics
```

Then in `create_app()`, add `setup_metrics(app)` as the last statement before `return app` (after all `include_router` calls so the instrumentator middleware wraps the full stack):

```python
    app.include_router(tools.router)

    setup_metrics(app)

    return app
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_observability.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 8: Lint, type-check, full suite**

Run:
```bash
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```
Expected: ruff clean, `Success: no issues found` from mypy, full suite green (the existing tests build the app many times — proving no duplicate-registration regression).

- [ ] **Step 9: Commit**

```bash
git add apps/api/src/usan_api/observability apps/api/src/usan_api/main.py apps/api/tests/test_observability.py
git commit -m "feat(api): expose Prometheus /metrics with instrumentator + custom counters (MON-2)"
```

---

### Task 3: Wire custom-counter increments into the routers

**Files:**
- Modify: `apps/api/src/usan_api/routers/webhooks.py`
- Modify: `apps/api/src/usan_api/routers/tools.py`

(The increment tests already live in `tests/test_observability.py` from Task 2: `test_tool_call_error_path_increments_counter` and `test_invalid_webhook_increments_counter`. They currently fail until the increments below exist — that is the RED state for this task.)

- [ ] **Step 1: Confirm the increment tests fail without the wiring**

Run: `cd apps/api && uv run pytest tests/test_observability.py -k "increments" -v`
Expected: the two `*_increments_counter` tests FAIL (counters never move; deltas are 0).

> If Task 2 was implemented and committed, these two were already failing/xfailing — re-run to confirm the RED state before wiring.

- [ ] **Step 2: Increment `usan_webhooks_total` in the webhooks handler**

In `apps/api/src/usan_api/routers/webhooks.py`, add the import (with the other `usan_api` imports):

```python
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL
```

In `livekit_webhook`, record `invalid` in BOTH `except` branches before raising, and `ok` on the success return. The two except blocks become:

```python
    except livekit_webhooks.WebhookReplayError as exc:
        logger.warning("Rejected replayed (stale) LiveKit webhook: {reason}", reason=str(exc))
        WEBHOOKS_TOTAL.labels(type="unknown", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
    except Exception as exc:  # invalid signature / hash mismatch / malformed
        WEBHOOKS_TOTAL.labels(type="unknown", outcome="invalid").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
```

And change the final `return` of `livekit_webhook` from:

```python
    return {"ok": True}
```

to:

```python
    WEBHOOKS_TOTAL.labels(type=event.event, outcome="ok").inc()
    return {"ok": True}
```

- [ ] **Step 3: Add `@track_tool` to every tool handler + `usan_calls_total` in `end_call`**

In `apps/api/src/usan_api/routers/tools.py`, add the import (with the other `usan_api` imports):

```python
from usan_api.observability.custom_metrics import CALLS_TOTAL, track_tool
```

Add a `@track_tool("<name>")` decorator **directly below** each `@router.post(...)` line and **above** the `async def`, for all six handlers. The names must match the handler:

```python
@router.post("/log_wellness", response_model=LoggedResponse)
@track_tool("log_wellness")
async def log_wellness(
```
```python
@router.post("/log_medication", response_model=LoggedResponse)
@track_tool("log_medication")
async def log_medication(
```
```python
@router.post("/get_today_meds", response_model=TodayMedsResponse)
@track_tool("get_today_meds")
async def get_today_meds(
```
```python
@router.post("/end_call", response_model=CallEndedResponse)
@track_tool("end_call")
async def end_call(
```
```python
@router.post("/log_transcript", response_model=TranscriptLoggedResponse)
@track_tool("log_transcript")
async def log_transcript(
```
```python
@router.post("/log_metrics", response_model=MetricsAcceptedResponse)
@track_tool("log_metrics")
async def log_metrics(
```

Then, in the `end_call` handler body, increment the calls counter using the BOUNDED status enum (never `body.reason` — it is PHI-bearing free-text). The handler body becomes:

```python
    call = await _authorize_call(body.call_id, claims, db)
    updated = await calls_repo.complete_call_if_in_progress(db, call.id, end_reason=body.reason)
    await db.commit()
    final = updated or call
    # Label value is the bounded call_status enum, NOT body.reason (free-text PHI).
    CALLS_TOTAL.labels(direction=final.direction.value, end_reason=final.status.value).inc()
    # Don't log body.reason: it's free-text the LLM fills, so it could carry clinical
    # content. It's already persisted to the DB (end_reason); the log keeps only call_id.
    logger.bind(call_id=str(call.id)).info("end_call requested")
    return CallEndedResponse(status=final.status.value)
```

- [ ] **Step 4: Run the increment tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_observability.py -k "increments" -v`
Expected: both `*_increments_counter` tests PASS.

- [ ] **Step 5: Lint, type-check, full suite**

Run:
```bash
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```
Expected: all green. (mypy must still pass with the decorated handlers — the `ParamSpec` decorator preserves signatures.)

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/webhooks.py apps/api/src/usan_api/routers/tools.py
git commit -m "feat(api): increment usan_{calls,webhooks,tool_calls}_total counters (MON-2)"
```

---

### Task 4: Block `/metrics` at the Caddy edge

**Files:**
- Modify: `infra/Caddyfile`

`/metrics` is scraped internally by the Prometheus container over the compose bridge (`api:8000/metrics`); it must NOT be reachable through Caddy's public `{$API_DOMAIN}` reverse proxy (spec §8). 403 (not 404) per spec §8/§10.

- [ ] **Step 1: Add the `/metrics` matcher to the API site block**

In `infra/Caddyfile`, edit the `{$API_DOMAIN}` block so the new matcher sits before the `reverse_proxy`:

```caddyfile
{$API_DOMAIN} {
	encode zstd gzip
	# The operator/management endpoints (/v1/elders, /v1/dnc, /v1/calls) are
	# authenticated at the app layer via OPERATOR_API_KEY. An operator may add a
	# defense-in-depth IP allowlist for those paths here (e.g. a matcher with
	# `remote_ip` + `abort`) — not required for correctness.
	#
	# /metrics is the internal Prometheus scrape endpoint (the prometheus
	# container hits api:8000/metrics over the bridge). It must never be served
	# at the public edge — block it here (Caddy otherwise proxies the whole host).
	@metrics path /metrics
	respond @metrics 403

	reverse_proxy api:8000 {
		# Overwrite X-Forwarded-For with the direct client so the API rate
		# limiter keys on the real (non-spoofable) caller IP, not this proxy's.
		header_up X-Forwarded-For {remote_host}
	}
}
```

- [ ] **Step 2: Validate the Caddyfile parses (with env substitution)**

Run from the repo root:
```bash
docker run --rm \
  -e API_DOMAIN=api.example.com \
  -e LIVEKIT_DOMAIN=lk.example.com \
  -v "$PWD/infra/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
```
Expected: `Valid configuration`.

- [ ] **Step 3: Commit**

```bash
git add infra/Caddyfile
git commit -m "feat(infra): block /metrics at the Caddy edge (MON-2)"
```

---

# PHASE 4 — Observability platform (spec §9, §10)

### Task 5: `grafana_ro` read-only DB role (Alembic migration 0009)

**Files:**
- Create: `apps/api/migrations/versions/0009_grafana_ro_role.py`
- Create: `apps/api/tests/test_grafana_ro_role.py`

The migration creates a **passwordless, idempotent** `grafana_ro` role and grants SELECT on exactly the six spec tables — **excluding `transcripts`** and using **no** `ALTER DEFAULT PRIVILEGES`. The login *password* is set out-of-band by Terraform's `google_sql_user.grafana_ro` (Task 10); the idempotent `CREATE ROLE` guard lets the migration coexist with the Terraform-managed role regardless of apply order. In dev/test the role stays passwordless (tests don't connect as it).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_grafana_ro_role.py`:

```python
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

# Migrations (incl. 0009) run once via the session-scoped `database_url`
# fixture's `alembic upgrade head`. Depending on `async_database_url` guarantees
# they have been applied.


async def test_grafana_ro_exists_and_can_log_in(async_database_url):
    engine = create_async_engine(async_database_url)
    try:
        async with engine.connect() as conn:
            rolcanlogin = await conn.scalar(
                sa.text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'grafana_ro'")
            )
        assert rolcanlogin is True
    finally:
        await engine.dispose()


async def test_grafana_ro_can_select_reporting_tables(async_database_url):
    engine = create_async_engine(async_database_url)
    allowed = ("calls", "elders", "wellness_logs", "medication_logs",
               "turn_metrics", "call_metrics")
    try:
        async with engine.connect() as conn:
            for table in allowed:
                granted = await conn.scalar(
                    sa.text("SELECT has_table_privilege('grafana_ro', :t, 'SELECT')").bindparams(t=table)
                )
                assert granted is True, f"grafana_ro should SELECT {table}"
    finally:
        await engine.dispose()


async def test_grafana_ro_cannot_read_transcripts_or_write(async_database_url):
    engine = create_async_engine(async_database_url)
    try:
        async with engine.connect() as conn:
            reads_transcripts = await conn.scalar(
                sa.text("SELECT has_table_privilege('grafana_ro', 'transcripts', 'SELECT')")
            )
            writes_calls = await conn.scalar(
                sa.text("SELECT has_table_privilege('grafana_ro', 'calls', 'INSERT')")
            )
        assert reads_transcripts is False, "grafana_ro must NOT read transcripts (raw PHI)"
        assert writes_calls is False, "grafana_ro must be read-only"
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_grafana_ro_role.py -v`
Expected: FAIL — `grafana_ro` does not exist, so `rolcanlogin` is `None` and `has_table_privilege` raises (role missing).

- [ ] **Step 3: Write the migration**

Create `apps/api/migrations/versions/0009_grafana_ro_role.py`:

```python
"""grafana_ro: read-only role + SELECT on the six reporting tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-06

The login password is NOT set here (committed migrations must carry no secrets).
It is provisioned out-of-band by Terraform (google_sql_user.grafana_ro). The role
is created idempotently so this migration coexists with the Terraform-managed role
regardless of apply order. Excludes `transcripts` (raw conversation PHI) and uses
no ALTER DEFAULT PRIVILEGES, so future tables are not auto-exposed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent create (CREATE ROLE has no IF NOT EXISTS); LOGIN, no password here.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
                CREATE ROLE grafana_ro LOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT CONNECT ON DATABASE usan TO grafana_ro")
    op.execute("GRANT USAGE ON SCHEMA public TO grafana_ro")
    # Least privilege: exactly the six reporting tables the dashboards read.
    # transcripts (raw conversation PHI) is intentionally NOT granted.
    op.execute(
        """
        GRANT SELECT ON
            calls, elders, wellness_logs, medication_logs, turn_metrics, call_metrics
        TO grafana_ro
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON
            calls, elders, wellness_logs, medication_logs, turn_metrics, call_metrics
        FROM grafana_ro
        """
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM grafana_ro")
    op.execute("REVOKE CONNECT ON DATABASE usan FROM grafana_ro")
    op.execute("DROP ROLE IF EXISTS grafana_ro")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_grafana_ro_role.py -v`
Expected: all three tests PASS (the testcontainer ran `alembic upgrade head` including 0009).

- [ ] **Step 5: Confirm the downgrade reverses cleanly**

Run from `apps/api` against a throwaway container (mirrors how conftest provisions one), driving Alembic directly:
```bash
cd apps/api && docker run -d --rm --name mon2pg -e POSTGRES_USER=usan -e POSTGRES_PASSWORD=usan -e POSTGRES_DB=usan -p 55432:5432 pgvector/pgvector:pg18
sleep 6
DATABASE_URL=postgresql://usan:usan@localhost:55432/usan LIVEKIT_API_KEY=k LIVEKIT_API_SECRET=$(python -c "print('a'*32)") LIVEKIT_URL=ws://l:7880 JWT_SIGNING_KEY=$(python -c "print('s'*32)") OPERATOR_API_KEY=$(python -c "print('o'*32)") uv run alembic upgrade head
DATABASE_URL=postgresql://usan:usan@localhost:55432/usan LIVEKIT_API_KEY=k LIVEKIT_API_SECRET=$(python -c "print('a'*32)") LIVEKIT_URL=ws://l:7880 JWT_SIGNING_KEY=$(python -c "print('s'*32)") OPERATOR_API_KEY=$(python -c "print('o'*32)") uv run alembic downgrade 0008
docker stop mon2pg
```
Expected: `upgrade` runs through `0009`; `downgrade 0008` runs `0009.downgrade()` with no error (role + grants dropped). Then `docker stop` cleans up.

- [ ] **Step 6: Lint + full suite**

Run: `cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add apps/api/migrations/versions/0009_grafana_ro_role.py apps/api/tests/test_grafana_ro_role.py
git commit -m "feat(api): grafana_ro read-only role + SELECT on reporting tables (MON-2)"
```

---

### Task 6: Prometheus scrape config

**Files:**
- Create: `infra/prometheus/prometheus.yml`

- [ ] **Step 1: Create the scrape config**

Create `infra/prometheus/prometheus.yml`:

```yaml
# Prometheus scrape config for the USAN voice engine (MON-2).
# Scrapes ONLY the API container over the compose bridge. /metrics is plaintext
# over the bridge; external TLS is Caddy's job (and /metrics is blocked at the edge).
global:
  scrape_interval: 15s
  scrape_timeout: 10s
  external_labels:
    monitor: usan-voice-engine

scrape_configs:
  - job_name: usan-api
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ["api:8000"]
        labels:
          service: api

  # Prometheus self-monitoring (cheap, useful for the System dashboard).
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
```

- [ ] **Step 2: Validate with promtool**

Run from the repo root:
```bash
docker run --rm \
  -v "$PWD/infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  prom/prometheus:v3.12.0 promtool check config /etc/prometheus/prometheus.yml
```
Expected: `Checking /etc/prometheus/prometheus.yml: SUCCESS` (no rule files is fine).

- [ ] **Step 3: Commit**

```bash
git add infra/prometheus/prometheus.yml
git commit -m "feat(infra): prometheus scrape config for api /metrics (MON-2)"
```

---

### Task 7: Grafana provisioning-as-code (datasources + dashboard provider)

**Files:**
- Create: `infra/grafana/provisioning/datasources/datasources.yml`
- Create: `infra/grafana/provisioning/dashboards/dashboards.yml`
- Create: `infra/grafana/dashboards/.gitkeep`

MON-2 ships the datasources + provider; the `dashboards/` dir is empty until MON-3. Grafana interpolates `${ENV}` in provisioning YAML from the container env (Task 8 supplies the values).

- [ ] **Step 1: Create the datasources file**

Create `infra/grafana/provisioning/datasources/datasources.yml`:

```yaml
apiVersion: 1

# ${...} values are interpolated by Grafana from the container environment
# (see infra/docker-compose.monitoring.yml). The Postgres datasource uses the
# read-only grafana_ro role (migration 0009 + terraform google_sql_user).
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    jsonData:
      httpMethod: POST
      timeInterval: 15s
    editable: false

  - name: Postgres
    uid: postgres-ro
    type: postgres
    access: proxy
    url: ${GRAFANA_DB_HOST}        # Cloud SQL private IP:5432 (prod) / postgres:5432 (dev)
    user: grafana_ro
    jsonData:
      database: usan
      sslmode: ${GRAFANA_DB_SSLMODE}   # require (prod) / disable (dev bridge)
      postgresVersion: 1500            # query-builder feature gate only; safe for PG18
    secureJsonData:
      password: ${GF_POSTGRES_RO_PASSWORD}
    editable: false
```

- [ ] **Step 2: Create the dashboard provider file**

Create `infra/grafana/provisioning/dashboards/dashboards.yml`:

```yaml
apiVersion: 1

# File-based dashboard provider. MON-3 drops *.json into the mounted dashboards
# dir; Grafana picks them up on the rescan interval. allowUiUpdates=false makes
# the checked-in JSON the source of truth (GitOps).
providers:
  - name: usan-dashboards
    orgId: 1
    folder: USAN
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
```

- [ ] **Step 3: Create the empty dashboards dir**

Create `infra/grafana/dashboards/.gitkeep`:

```
```
(empty file — keeps the otherwise-empty dir in git; MON-3 fills it)

- [ ] **Step 4: Validate the provisioning YAML parses**

Run from the repo root (uses an ephemeral PyYAML, no project changes):
```bash
uv run --no-project --with pyyaml python -c "
import glob, yaml
for f in glob.glob('infra/grafana/provisioning/**/*.yml', recursive=True):
    with open(f) as fh:
        yaml.safe_load(fh)
    print('ok', f)
"
```
Expected: prints `ok infra/grafana/provisioning/.../datasources.yml` and `... dashboards.yml` (both parse).

- [ ] **Step 5: Commit**

```bash
git add infra/grafana
git commit -m "feat(infra): grafana datasource + dashboard provisioning (MON-2)"
```

---

### Task 8: Prometheus + Grafana compose overlay

**Files:**
- Create: `infra/docker-compose.monitoring.yml`
- Modify: `infra/.env.prod.example`

Both services on the default bridge (so Prometheus resolves `api:8000` and Grafana resolves `prometheus:9090`). Grafana has **no published port** — only Caddy reaches it (Task 9). This overlay is prod-only (like `docker-compose.tls.yml`); `journald` logging matches api/agent so the logs reach Cloud Logging.

- [ ] **Step 1: Create the monitoring overlay**

Create `infra/docker-compose.monitoring.yml`:

```yaml
# Monitoring overlay (prod) — Prometheus (scrapes the API) + Grafana (behind Caddy).
# Layer AFTER the tls overlay:
#   docker compose --env-file infra/.env \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.prod.yml \
#     -f infra/docker-compose.tls.yml \
#     -f infra/docker-compose.monitoring.yml up -d
#
# Requires in .env: GF_SECURITY_ADMIN_PASSWORD, GF_POSTGRES_RO_PASSWORD,
# GRAFANA_DB_HOST, GRAFANA_DB_SSLMODE, GRAFANA_DOMAIN (+ GRAFANA_ALLOWED_CIDR for Caddy).
services:
  prometheus:
    image: prom/prometheus:v3.12.0
    container_name: usan-prometheus
    init: true
    user: "65534:65534" # nobody; the image runs non-root
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--storage.tsdb.retention.time=30d"
      - "--storage.tsdb.retention.size=5GB"
      - "--web.listen-address=0.0.0.0:9090"
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    # No published port: scraped internally; reach the UI via SSH tunnel if needed.
    restart: unless-stopped
    logging:
      driver: journald

  grafana:
    image: grafana/grafana:12.4.4
    container_name: usan-grafana
    init: true
    depends_on:
      - prometheus
    environment:
      GF_SERVER_HTTP_PORT: "3000"
      GF_SERVER_ROOT_URL: "https://${GRAFANA_DOMAIN}"
      GF_SERVER_SERVE_FROM_SUB_PATH: "false"
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_SECURITY_ADMIN_PASSWORD: ${GF_SECURITY_ADMIN_PASSWORD}
      # Consumed by datasources.yml provisioning interpolation:
      GF_POSTGRES_RO_PASSWORD: ${GF_POSTGRES_RO_PASSWORD}
      GRAFANA_DB_HOST: ${GRAFANA_DB_HOST}
      GRAFANA_DB_SSLMODE: ${GRAFANA_DB_SSLMODE:-require}
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    # No published port: Caddy reverse-proxies grafana:3000 (operator-CIDR gated).
    restart: unless-stopped
    logging:
      driver: journald

volumes:
  prometheus_data:
  grafana_data:
```

- [ ] **Step 2: Add the new keys to the env template**

In `infra/.env.prod.example`, append a new section at the end (after the recording block):

```bash

# === Observability — Grafana + Prometheus (MON-2) ===
# Grafana is reverse-proxied by Caddy at GRAFANA_DOMAIN and gated to an operator
# CIDR allowlist at L7 (GRAFANA_ALLOWED_CIDR). Prometheus is internal-only.
GRAFANA_DOMAIN=grafana.usan.example
# Space-separated CIDR allowlist for the Grafana subdomain (Caddy remote_ip).
# Use /32 for a single host, e.g. "203.0.113.4/32".
GRAFANA_ALLOWED_CIDR=203.0.113.4/32
# Grafana admin login password. Generate: terraform output -raw grafana_admin_password
GF_SECURITY_ADMIN_PASSWORD=__GRAFANA_ADMIN_PASSWORD__
# Read-only Postgres role password (grafana_ro). terraform output -raw grafana_ro_password
GF_POSTGRES_RO_PASSWORD=__GRAFANA_RO_DB_PASSWORD__
# Postgres host:port for the Grafana datasource. Prod = Cloud SQL private IP:5432
# (the same host as DATABASE_URL); dev = postgres:5432.
GRAFANA_DB_HOST=__CLOUD_SQL_PRIVATE_IP__:5432
# require in prod (Cloud SQL); disable for the dev bridge container.
GRAFANA_DB_SSLMODE=require
```

- [ ] **Step 3: Validate the overlay merges cleanly**

Run from the repo root with a throwaway env file (`config` validates syntax + merge without starting anything):
```bash
printf 'IMAGE_TAG=test\nAPI_DOMAIN=api.example.com\nLIVEKIT_DOMAIN=lk.example.com\nGRAFANA_DOMAIN=grafana.example.com\nGF_SECURITY_ADMIN_PASSWORD=x\nGF_POSTGRES_RO_PASSWORD=x\nGRAFANA_DB_HOST=10.0.0.2:5432\nGRAFANA_DB_SSLMODE=require\n' > /tmp/mon.env
docker compose --env-file /tmp/mon.env \
  -f infra/docker-compose.yml -f infra/docker-compose.prod.yml \
  -f infra/docker-compose.tls.yml -f infra/docker-compose.monitoring.yml \
  config >/dev/null && echo "compose config OK"
rm -f /tmp/mon.env
```
Expected: `compose config OK` (no `prometheus`/`grafana` schema errors; both services present, both volumes declared).

- [ ] **Step 4: Commit**

```bash
git add infra/docker-compose.monitoring.yml infra/.env.prod.example
git commit -m "feat(infra): prometheus + grafana compose overlay + env template (MON-2)"
```

---

### Task 9: Caddy Grafana subdomain (operator-CIDR gated)

**Files:**
- Modify: `infra/Caddyfile`
- Modify: `infra/docker-compose.tls.yml`

- [ ] **Step 1: Add the Grafana site block**

Append to `infra/Caddyfile` (after the `{$LIVEKIT_DOMAIN}` block). Grafana is a bridge service, so Caddy reaches it by compose DNS name `grafana:3000` (unlike host-networked livekit). Caddy auto-issues a Let's Encrypt cert for the new hostname.

```caddyfile
{$GRAFANA_DOMAIN} {
	encode zstd gzip
	# Operator-CIDR allowlist enforced at L7 (443 is shared across api/lk/grafana
	# by SNI, so this cannot be a VM firewall rule). Anyone outside the CIDR gets
	# 403; allowed clients hit Grafana (which also requires its own login).
	@operator remote_ip {$GRAFANA_ALLOWED_CIDR}
	handle @operator {
		reverse_proxy grafana:3000
	}
	respond 403
}
```

- [ ] **Step 2: Plumb the two new env vars into the caddy service**

In `infra/docker-compose.tls.yml`, extend the caddy service `environment:` map:

```yaml
    environment:
      API_DOMAIN: ${API_DOMAIN}
      LIVEKIT_DOMAIN: ${LIVEKIT_DOMAIN}
      GRAFANA_DOMAIN: ${GRAFANA_DOMAIN}
      GRAFANA_ALLOWED_CIDR: ${GRAFANA_ALLOWED_CIDR}
```

- [ ] **Step 3: Validate the Caddyfile parses with the new block**

Run from the repo root:
```bash
docker run --rm \
  -e API_DOMAIN=api.example.com \
  -e LIVEKIT_DOMAIN=lk.example.com \
  -e GRAFANA_DOMAIN=grafana.example.com \
  -e GRAFANA_ALLOWED_CIDR="203.0.113.4/32 198.51.100.0/24" \
  -v "$PWD/infra/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
```
Expected: `Valid configuration`.

- [ ] **Step 4: Commit**

```bash
git add infra/Caddyfile infra/docker-compose.tls.yml
git commit -m "feat(infra): caddy grafana subdomain with operator-CIDR allowlist (MON-2)"
```

---

### Task 10: Terraform — DNS record, grafana_ro user, generated passwords

**Files:**
- Modify: `infra/terraform/dns.tf`
- Modify: `infra/terraform/database.tf`
- Modify: `infra/terraform/main.tf`
- Modify: `infra/terraform/outputs.tf`

The CIDR is enforced in Caddy (`.env`), so no firewall rule / TF variable is added for it. We add: the DNS record, the `grafana_ro` Cloud SQL user with a generated password, a generated Grafana admin password, sensitive outputs, and the optional `roles/monitoring.viewer` IAM (prep for MON-3's host-metrics datasource).

- [ ] **Step 1: Add the Grafana DNS A-record**

Append to `infra/terraform/dns.tf` (mirrors `cloudflare_dns_record.api`):

```hcl
resource "cloudflare_dns_record" "grafana" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "grafana" # -> grafana.<zone domain>
  type    = "A"
  content = google_compute_address.usan.address
  proxied = false
  ttl     = 300
}
```

- [ ] **Step 2: Add the grafana_ro Cloud SQL user + generated password**

Append to `infra/terraform/database.tf` (mirrors `random_password.db` + `google_sql_user.usan`):

```hcl
# --- Read-only role login for Grafana (GRANTs live in Alembic migration 0009). ---
resource "random_password" "grafana_ro" {
  length  = 32
  special = false # avoids escaping issues in the .env datasource password
}

resource "google_sql_user" "grafana_ro" {
  name     = "grafana_ro"
  instance = google_sql_database_instance.usan.name
  password = random_password.grafana_ro.result
}
```

- [ ] **Step 3: Add the Grafana admin password + optional monitoring.viewer IAM**

Append to `infra/terraform/main.tf`:

```hcl
# --- Grafana admin login password (folded into the prod .env blob, like db_password). ---
resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

# Optional: lets a Grafana Cloud Monitoring datasource (MON-3 System dashboard)
# read host CPU/mem/disk. The VM SA currently has metricWriter only.
resource "google_project_iam_member" "vm_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.vm.email}"
}
```

- [ ] **Step 4: Add sensitive outputs for the two passwords**

Append to `infra/terraform/outputs.tf` (mirrors `output "db_password"`):

```hcl
output "grafana_admin_password" {
  description = "Generated Grafana admin password. Read: terraform output -raw grafana_admin_password — set GF_SECURITY_ADMIN_PASSWORD in the prod .env."
  value       = random_password.grafana_admin.result
  sensitive   = true
}

output "grafana_ro_password" {
  description = "Generated grafana_ro DB password. Read: terraform output -raw grafana_ro_password — set GF_POSTGRES_RO_PASSWORD in the prod .env."
  value       = random_password.grafana_ro.result
  sensitive   = true
}
```

- [ ] **Step 5: Validate formatting and config**

Run from `infra/terraform`:
```bash
cd infra/terraform && terraform fmt -check && terraform validate
```
Expected: `fmt -check` reports no diffs; `terraform validate` prints `Success! The configuration is valid.`
(If `terraform` is unavailable locally, run `terraform fmt -check` only, and note that `validate`/`plan` is run by the operator before `apply`.)

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/dns.tf infra/terraform/database.tf infra/terraform/main.tf infra/terraform/outputs.tf
git commit -m "feat(infra): terraform grafana DNS + grafana_ro user + admin/RO passwords (MON-2)"
```

---

### Task 11: Deploy wiring + runbook

**Files:**
- Modify: `.github/workflows/build.yml`
- Modify: `infra/README.md`

The deploy job currently scp's a fixed file list and runs a 3-overlay `docker compose up`. MON-2 adds a fourth overlay plus the prometheus/grafana config dirs.

- [ ] **Step 1: Ship the new files to the VM**

In `.github/workflows/build.yml`, in the **"Copy compose files to VM"** step, extend the `source:` list to include the overlay and the two config trees:

```yaml
          source: "infra/docker-compose.yml,infra/docker-compose.prod.yml,infra/docker-compose.tls.yml,infra/docker-compose.monitoring.yml,infra/Caddyfile,infra/prometheus,infra/grafana,infra/provision-sip-inbound.sh"
```

- [ ] **Step 2: Add the overlay to the prod compose invocation**

In the **"Pull images and bring stack up"** step, add the monitoring overlay to the `COMPOSE` variable (after the tls overlay):

```bash
            COMPOSE="docker compose --env-file infra/.env \
              -f infra/docker-compose.yml \
              -f infra/docker-compose.prod.yml \
              -f infra/docker-compose.tls.yml \
              -f infra/docker-compose.monitoring.yml"
```

- [ ] **Step 3: Document the runbook**

In `infra/README.md`, add a "Monitoring (Grafana / Prometheus)" subsection:

```markdown
## Monitoring (Grafana + Prometheus) — MON-2

Prometheus scrapes `api:8000/metrics` over the bridge (the endpoint is 403'd at
the public edge). Grafana is reverse-proxied by Caddy at `grafana.<domain>`,
gated to `GRAFANA_ALLOWED_CIDR` and requiring login.

One-time setup before the first deploy that includes the monitoring overlay:

1. `terraform apply` (creates the `grafana_ro` Cloud SQL user, the generated
   passwords, and the `grafana.<domain>` DNS record).
2. Fold the new values into the `usan-prod-env` Secret Manager secret (the `.env`):
   - `GRAFANA_DOMAIN=grafana.<domain>`
   - `GRAFANA_ALLOWED_CIDR=<your office/VPN CIDR>`
   - `GF_SECURITY_ADMIN_PASSWORD=$(terraform output -raw grafana_admin_password)`
   - `GF_POSTGRES_RO_PASSWORD=$(terraform output -raw grafana_ro_password)`
   - `GRAFANA_DB_HOST=<cloud sql private ip>:5432` (same host as DATABASE_URL)
   - `GRAFANA_DB_SSLMODE=require`
3. Ensure migration `0009` has been applied to Cloud SQL (the GRANTs for
   `grafana_ro`) via the same path that applies all migrations to prod.
4. Cut a `v*` tag → the deploy workflow ships the overlay and brings up
   `prometheus` + `grafana`.

Verify post-deploy:
- `curl -fsS https://api.<domain>/metrics` → **403** (edge-blocked; good).
- From an allowlisted IP, open `https://grafana.<domain>` → Grafana login.
- From a non-allowlisted IP → **403**.
- In Grafana → Connections → Data sources: Prometheus and Postgres both "working".
```

- [ ] **Step 4: Lint the workflow YAML**

Run from the repo root:
```bash
uv run --no-project --with pyyaml python -c "import yaml; yaml.safe_load(open('.github/workflows/build.yml')); print('build.yml OK')"
```
Expected: `build.yml OK`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/build.yml infra/README.md
git commit -m "ci(infra): deploy prometheus + grafana overlay; monitoring runbook (MON-2)"
```

---

## Final verification (run before opening the PR)

- [ ] **API suite + lint + types green:**
  ```bash
  cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
  ```
  Expected: ruff clean, `Success: no issues found` (mypy), all tests pass, coverage on new `observability/` + migration ≥ 80%.

- [ ] **Infra configs validate:**
  - `promtool check config` → SUCCESS (Task 6).
  - `caddy validate` with all 4 domains set → Valid configuration (Tasks 4 + 9).
  - `docker compose ... config` with the monitoring overlay → OK (Task 8).
  - `terraform fmt -check && terraform validate` → clean (Task 10).
  - Grafana provisioning YAML parses (Task 7).

- [ ] **`/metrics` shape sanity (manual):** with the stack up locally (dev compose + the API), `curl localhost:8000/metrics | grep usan_` shows the three counter families and `http_request*` RED metrics.

- [ ] **No PHI / no high-cardinality labels:** confirm no counter uses `call_id`, elder identifiers, phone numbers, or free-text reasons as labels (grep the `.labels(` call sites).

---

## Spec coverage map (self-review)

| Spec item | Task(s) |
|---|---|
| §8 instrumentator `/metrics` | 1, 2 |
| §8 custom counters `usan_calls_total`, `usan_webhooks_total`, `usan_tool_calls_total` | 2, 3 |
| §8/§10 Caddy `/metrics` 403 | 4 |
| §9 `grafana_ro` SELECT on the six tables (least privilege) | 5 |
| §8 Prometheus container + scrape + retention | 6, 8 |
| §9 Grafana provisioning (datasources + provider) | 7 |
| §10 Prometheus + Grafana containers + named volumes | 8 |
| §10 Caddy `grafana.<domain>` + operator-CIDR (`GRAFANA_ALLOWED_CIDR`) | 8, 9 |
| §10 DNS `grafana.<domain>` → VM IP | 10 |
| §10 Terraform secrets (admin + grafana_ro) folded into `usan-prod-env` | 8, 10, 11 |
| §10 optional `roles/monitoring.viewer` | 10 |
| §11 `/metrics` exposure + counter-increment tests | 2, 3 |
| §11 `promtool check config`; datasource provisioning validation | 6, 7 |
| §11 Caddy `/metrics` + remote_ip = integration/manual check | 4, 9, 11 (runbook) |
| §10 deploy (overlay shipped + composed) | 11 |

**Deferred to MON-3 (not gaps):** the four dashboard JSONs (§9 catalog), the Cloud Monitoring datasource wiring (§9), and SQL percentile/cost panels (§7 example query). MON-2 stands up everything those need: the Prometheus + read-only Postgres datasources, the `grafana_ro` grants, and the dashboard provider folder.
