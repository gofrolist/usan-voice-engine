# Monitoring Dashboard — Design

**Status:** Draft for review
**Date:** 2026-06-05
**Author:** Brainstormed with Claude Code
**Related:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` (§6 latency targets, §11 observability)

## 1. Goal

Give engineering/ops a single Grafana dashboard surface that shows **latency**, **cost**, and **business metrics** for the USAN voice engine. Today none of the latency or cost data is instrumented, and there is no dashboard or frontend of any kind. This project builds the missing telemetry *and* the dashboards in one effort.

## 2. Locked decisions

These were settled during brainstorming and are not open for re-litigation in the plan:

| Decision | Choice | Rationale |
|---|---|---|
| Scope | Build everything (latency + cost instrumentation **and** business dashboards) | Operator wants all three pillars, not a phased subset |
| Tooling | Self-hosted **Grafana** on the VM | Spec already names Prometheus/Loki path; Grafana unifies SQL + metrics |
| Audience | Engineering / ops | Technical panels acceptable; no bespoke business UI needed |
| Cost model | **Modeled** (usage × pricing), computed **server-side in the API** | Real-time, per-call/per-elder attributable; single source of pricing truth; re-priceable |
| Access | Operator-CIDR allowlist + TLS + Grafana auth | Dashboards surface PHI-adjacent data (mood/pain/meds) |
| OpenTelemetry | **None** | Cost/business are relational (Postgres); latency percentiles are simpler in SQL; tracing deferred entirely |
| Latency/cost storage | **Postgres** per-turn + per-call rows; Prometheus only for API RED metrics | Dodges the livekit-agents per-call **subprocess** problem; gives true percentiles + per-elder drill-down with zero extra infra |

## 3. Architecture: three data planes, one Grafana

```
                       ┌─────────────── Grafana (VM, behind Caddy/TLS) ───────────────┐
                       │   Latency · Cost · Business/Care · System  (4 dashboards)     │
                       └───────┬──────────────────┬───────────────────────┬───────────┘
                          Postgres DS        Prometheus DS         Cloud Monitoring DS
                               │                  │                        │
   per-call latency + cost + business      live RED metrics          host CPU/mem/disk
   (calls, wellness, meds, NEW tables)     (API /metrics)            (already provisioned)
                               ▲                  ▲
   services/agent ──HTTP POST──► apps/api ────────┘
   (taps LiveKit metrics)        (computes cost, persists, exposes /metrics)
```

Why this shape:

- **Postgres is the workhorse.** Per-turn latency samples + per-call cost + all business data live here. Headline SLIs (e.g. *end-of-turn p95*) come from SQL `percentile_cont` over per-turn rows — true percentiles with per-call/per-elder drill-down.
- **The agent never runs Prometheus.** livekit-agents runs each call in its own **subprocess**; a scrape endpoint there would need a Pushgateway or `PROMETHEUS_MULTIPROC_DIR`. Instead the agent taps LiveKit's `metrics_collected` events and POSTs a compact summary to the API at call end — reusing the JWT-authenticated HTTP path it already uses for outcomes/transcripts.
- **Prometheus is scoped to the API only** (live RED metrics: request rate/latency/5xx, webhook + tool health). One stable process, trivial to scrape. One container, ~30-day retention.
- **Cloud Monitoring = host/infra** (CPU/mem/disk/uptime), already provisioned in `infra/terraform/observability.tf`; Grafana reads it via the Google Cloud Monitoring datasource.

## 4. Component A — Agent instrumentation (`services/agent`)

New module (e.g. `src/usan_agent/metrics_hooks.py`):

- Register `@session.on("metrics_collected")` on the `AgentSession`.
- Per **turn**, capture from the event payload (`livekit.agents.metrics`):
  - `EOUMetrics.end_of_utterance_delay`, `EOUMetrics.transcription_delay`
  - `STTMetrics.duration`
  - `LLMMetrics.ttft`, `LLMMetrics.completion_tokens`
  - `TTSMetrics.ttfb`, `TTSMetrics.characters_count`
- Accumulate session totals with `metrics.UsageCollector` → `UsageSummary` (llm prompt/completion tokens, tts characters, stt audio seconds).
- At session end (shutdown hook), POST to the API:

```json
{
  "turns": [
    {"turn_index": 0, "eou_delay_ms": 180, "transcription_delay_ms": 120,
     "stt_duration_ms": 90, "llm_ttft_ms": 210, "tts_ttfb_ms": 80,
     "llm_completion_tokens": 64, "tts_characters": 240}
  ],
  "usage": {"llm_prompt_tokens": 1200, "llm_completion_tokens": 800,
            "tts_characters": 3400, "stt_audio_seconds": 95.2}
}
```

> **Verification item (V1):** the exact LiveKit symbol/field names above are from the livekit-agents 1.x source and **must be confirmed against the pinned version** (`grep` the installed package / `livekit.agents.metrics`) before implementing. Likewise confirm native availability of the `metrics_collected` event and `UsageCollector` in the pinned release.

The agent computes **no cost** — it sends raw usage + latency only. Cost is computed in one place (the API).

## 5. Component B — Ingestion + storage (`apps/api`)

### 5.1 Endpoint

`POST /v1/calls/{call_id}/metrics`

- Auth: the **same JWT dependency** used by `/v1/calls/{id}/outcome` (agent-issued token).
- Body: Pydantic model matching §4 (validated at the boundary; reject unknown/oversized payloads — cap `turns` length).
- Idempotent: at most one `call_metrics` row per call; turns are inserted once (guard on existing rows / upsert).
- On receipt: insert `turn_metrics` rows, compute cost (§6) using the call's own `duration_seconds`, insert/upsert the `call_metrics` row.

### 5.2 New tables (Alembic migration)

Neither table contains PHI — latency/cost/usage only.

```sql
CREATE TABLE turn_metrics (
    id                     BIGSERIAL PRIMARY KEY,
    call_id                UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index             INTEGER NOT NULL,
    eou_delay_ms           INTEGER,
    transcription_delay_ms INTEGER,
    stt_duration_ms        INTEGER,
    llm_ttft_ms            INTEGER,
    tts_ttfb_ms            INTEGER,
    llm_completion_tokens  INTEGER,
    tts_characters         INTEGER,
    response_latency_ms    INTEGER,   -- computed composite, see §7
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_turn_metrics_call    ON turn_metrics (call_id);
CREATE INDEX idx_turn_metrics_created ON turn_metrics (created_at);

CREATE TABLE call_metrics (
    call_id               UUID PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    llm_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    llm_completion_tokens INTEGER NOT NULL DEFAULT 0,
    llm_total_tokens      INTEGER NOT NULL DEFAULT 0,
    tts_characters        INTEGER NOT NULL DEFAULT 0,
    stt_audio_seconds     NUMERIC(10,2) NOT NULL DEFAULT 0,
    duration_seconds      INTEGER,                       -- snapshot from calls at compute time
    cost_telephony_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_llm_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_stt_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_tts_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_storage_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_total_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
    pricing_version       TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Per-elder cost/latency comes from joining `call_metrics`/`turn_metrics` → `calls.elder_id`.

> **PHI/retention note:** these tables hold no PHI, so they can be retained long-term. The existing `PHI_RETENTION` purge already governs wellness/meds/transcripts; `ON DELETE CASCADE` from `calls` keeps these consistent if a call row is ever deleted.

## 6. Cost model (modeled, server-side)

Pricing lives in config (`settings.py`, validated at startup), versioned via `pricing_version` so historical rows are reproducible and re-priceable.

| Component | Formula | Source of usage | Constant (default) |
|---|---|---|---|
| Telephony | `duration_seconds/60 × telnyx_per_min` | `calls.duration_seconds` | `0.008` (spec §2, blended US) |
| LLM | `prompt_tokens/1000 × in_rate + completion_tokens/1000 × out_rate` | agent usage | **placeholder** — Vertex Gemini Flash-Lite rates (V2) |
| STT | `stt_audio_seconds/60 × cartesia_stt_per_min` | agent usage | **placeholder** — Cartesia (V2) |
| TTS | `tts_characters/1000 × cartesia_tts_per_1k_chars` | agent usage | **placeholder** — Cartesia (V2) |
| Storage | `recording_bytes/1e9 × gcs_per_gb_month` (amortized) | recording size / duration est. | **placeholder** (V2); small — may approximate |
| Fixed infra | `fixed_infra_monthly / calls_this_month` (optional blended line) | config | `50–150/mo` (spec §2) |

- `cost_total_usd = telephony + llm + stt + tts + storage`.
- A configurable `retell_baseline_per_min` constant drives the "vs RetellAI" comparison line on the Cost dashboard.

> **Verification item (V2):** fill LLM/STT/TTS/GCS pricing constants from current vendor pricing before trusting cost numbers. Until filled, cost panels are directional.

## 7. Latency definition

`response_latency_ms` (stored per turn) is the dashboard's headline responsiveness metric, defined as the user-perceived gap from end-of-speech to first audio out:

```
response_latency_ms ≈ transcription_delay_ms + llm_ttft_ms + tts_ttfb_ms
```

Target: **p95 ≤ 1200 ms** (spec §6). `end_of_utterance_delay` is tracked separately (detector behaviour) to avoid double-counting silence-detection time.

> **Verification item (V3):** confirm against LiveKit's own metric definitions that these components don't overlap (e.g. whether `transcription_delay` already includes STT duration) before finalizing the composite, to avoid double counting. Adjust the formula in the migration/compute if needed.

Example percentile query (Grafana Postgres datasource macros):

```sql
SELECT $__timeGroup(created_at, '1h') AS time,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY response_latency_ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY response_latency_ms) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY response_latency_ms) AS p99
FROM turn_metrics
WHERE $__timeFilter(created_at)
GROUP BY 1 ORDER BY 1;
```

## 8. Component C — Prometheus (API RED metrics)

- `prometheus-fastapi-instrumentator` exposes `/metrics` with `http_request_duration_seconds` etc.
- Custom counters: `usan_calls_total{direction,end_reason}`, `usan_webhooks_total{type,outcome}`, `usan_tool_calls_total{tool,outcome}`.
- `/metrics` is **internal only** — scraped by the Prometheus container over the compose bridge network (`api:8000/metrics`). It must **not** be publicly reachable: add a Caddy rule on the API domain to `respond 403` (or `not remote_ip`) for the `/metrics` path, since Caddy otherwise proxies the whole API host.
- Prometheus config: single scrape target (the API), retention ~30 days, local TSDB volume.

## 9. Component D — Grafana (provisioned as code)

Datasources and dashboards are checked into the repo so they are reproducible, not click-ops.

```
infra/grafana/
  provisioning/
    datasources/datasources.yml   # Postgres (read-only user), Prometheus, Cloud Monitoring (optional)
    dashboards/dashboards.yml      # provider → /var/lib/grafana/dashboards
  dashboards/
    latency.json
    cost.json
    business.json
    system.json
```

- Grafana connects to Postgres with a **dedicated read-only role** (`grafana_ro`, SELECT on `calls`, `elders`, `wellness_logs`, `medication_logs`, `turn_metrics`, `call_metrics` only) — least privilege, no write/scrub capability.
- Cloud Monitoring datasource is optional; if used, the VM service account needs `roles/monitoring.viewer` added (it currently has `metricWriter` only).

### Dashboard catalog

| Dashboard | Source | Headline panels |
|---|---|---|
| **Latency** | Postgres | end-of-turn p50/p95/p99 over time (1200 ms target line); per-stage STT duration / LLM ttft / TTS ttfb percentiles; worst-calls table (drill to `call_id`); per-turn distribution |
| **Cost** | Postgres | cost/call (avg, p95); daily/monthly spend stacked by component; blended $/min vs RetellAI baseline; cost per elder (top N); projected monthly spend |
| **Business/Care** | Postgres | call volume in/out; success / no-answer / voicemail / failed / DNC rates; retry effectiveness by attempt; avg duration; mood & pain trends; medication adherence % |
| **System (RED)** | Prometheus + Cloud Monitoring | API req rate / latency / 5xx; webhook + tool-endpoint latency & errors; host CPU/mem/disk; service up |

## 10. Component E — Deployment, access, security

- **Containers** (prod compose overlay): `prometheus` + `grafana`, each with a named volume. Grafana env: `GF_SECURITY_ADMIN_PASSWORD` (from Secret Manager/.env), `GF_SERVER_ROOT_URL`, `GF_AUTH_ANONYMOUS_ENABLED=false`, default org role Viewer.
- **Caddy**: new `grafana.<domain>` block — TLS + `@operator remote_ip <CIDR>` matcher (403 otherwise) → `reverse_proxy grafana:3000`. The CIDR is supplied via env (`GRAFANA_ALLOWED_CIDR`). Because 443 is shared (api/livekit/grafana via Caddy SNI), the allowlist is enforced at L7 in Caddy, not via a VM firewall rule. Also add the `/metrics` 403 rule (§8).
- **DNS**: A record for `grafana.<domain>` → VM static IP (Cloudflare `dns.tf`, `proxied=false`, or manual).
- **Terraform**: new vars (`grafana_allowed_cidr`, optional `roles/monitoring.viewer`), secrets for Grafana admin + `grafana_ro` DB password folded into the existing `usan-prod-env` secret.
- **PHI posture**: business dashboards lean aggregate; per-elder drill-downs exist but are gated behind operator-CIDR + auth + TLS. No PHI is logged into panel titles/annotations.

## 11. Testing

- **`apps/api`**: `/v1/calls/{id}/metrics` — auth required (401 without JWT), payload validation, persistence of turns + call_metrics, idempotency; cost-model unit tests (each formula + `pricing_version`); Alembic upgrade/downgrade; `/metrics` exposure + custom counters increment.
- **`services/agent`**: metrics accumulation from synthetic `MetricsCollectedEvent` objects; payload building; POST client (mocked) — including a call with zero turns.
- **Infra**: validate Grafana dashboard JSON (schema/lint) and datasource provisioning; `promtool check config` for `prometheus.yml`. The Caddy `/metrics` block and remote_ip allowlist are verified by an integration/manual check (noted, not unit-tested).
- Coverage target: 80%+ on new API + agent code (repo standard).

## 12. Implementation phasing (one spec, sequenced — each phase shippable)

1. **API storage + ingestion**: migration (both tables), `/metrics` endpoint, cost model, settings/pricing config, tests.
2. **Agent instrumentation**: LiveKit metrics tap, usage collection, end-of-session POST, tests. (Resolve V1/V3 here.)
3. **API Prometheus**: instrumentator `/metrics` + custom counters; Caddy `/metrics` 403.
4. **Infra**: Prometheus + Grafana containers, `grafana_ro` DB role, Caddy grafana subdomain + CIDR, Terraform vars/secrets/DNS, optional `monitoring.viewer`.
5. **Dashboards-as-code**: the four Grafana dashboards + datasource provisioning.

## 13. Out of scope (for now)

- Real-time live-call supervisor / barge-in view.
- Actual-billed cost reconciliation (modeled cost only; no GCP Billing/BigQuery/Telnyx usage-API integration).
- Loki/log-content dashboards (logs stay in Cloud Logging).
- OpenTelemetry tracing/metrics (explicitly dropped).
- New alerting beyond the existing host/uptime alerts in `observability.tf`.

## 14. Open verification items (resolve during implementation)

- **V1** — confirm LiveKit `metrics_collected` event, metric dataclass field names, and `UsageCollector` against the pinned `livekit-agents` version.
- **V2** — fill LLM/STT/TTS/GCS pricing constants from current vendor pricing.
- **V3** — validate the `response_latency_ms` composite against LiveKit's metric definitions (no double-counting).

## 15. Risks

- **Latency field semantics** (V3) — wrong composite → misleading p95. Mitigated by validating before finalizing and storing raw components so the composite can be recomputed.
- **Pricing drift / accuracy** (V2) — modeled cost is only as good as constants; `pricing_version` makes re-pricing safe.
- **VM headroom** — two more containers on `e2-standard-4`; Prometheus+Grafana are light at this scale, but watch memory after rollout.
- **PHI exposure via Grafana** — mitigated by operator-CIDR + auth + TLS + read-only DB role + aggregate-first panels.
- **`/metrics` public leak** — mitigated by the Caddy 403 rule; must be verified post-deploy.
