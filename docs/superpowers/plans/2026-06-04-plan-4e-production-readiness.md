# Plan 4e — Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every gate that must shut before real elder PHI flows through the system — the one active PHI leak (Gemini Developer API), the vendor BAAs, the missing audit-trail durability control — and harden the media plane.

**Architecture:** Three engineering workstreams (LLM→Vertex migration, observability/audit durability, media-plane host networking) plus a legal/procurement gate (BAAs). The engineering work ships as its own PRs on `v*` tags; the legal items are a go-live checklist that does not block code but blocks go-live.

**Tech Stack:** LiveKit Agents (Python 3.12), livekit-plugins-google (Vertex AI path), Google Cloud Ops Agent, Cloud Logging/Monitoring, Terraform, Docker Compose prod overlay.

---

## Status & the one hard stop

**Verified active finding (CRITICAL):** `services/agent/src/usan_agent/pipeline.py:52-55` calls `google.LLM(api_key=settings.gemini_api_key, model="gemini-3.1-flash-lite")`. With `api_key` set and no `vertexai` flag, livekit-plugins-google builds `google-genai` against `https://generativelanguage.googleapis.com/` (the **Gemini Developer API / AI Studio**), which is **not** on Google Cloud's HIPAA covered-products list and whose terms prohibit PHI + clinical use. The full conversation egresses there.

> **DO NOT run real or realistic elder data through the pipeline until Task A1 ships and the Google BAA scope (Task A4) is confirmed.** No actual patient PHI has flowed yet (the only live call was an internal test number), so this is a pre-launch gate, not an incident.

**Blocker / hardening split:**
- **Go-live blockers:** A1–A2 (Vertex migration, code+IAM), A3–A5 (BAA executions: Google scope, Cartesia, Telnyx), B (audit-log durability — HIPAA §164.312(b)).
- **Hardening (not a PHI gate):** C (media networking), D (Cloudflare token rotation), E (registry → GAR + keyless supply chain).

---

## Workstream A — Compliance / BAA (gates go-live)

### Task A1: Migrate the LLM from Gemini Developer API to Vertex AI

**Files:**
- Modify: `services/agent/src/usan_agent/pipeline.py:52-58`
- Modify: `services/agent/src/usan_agent/settings.py` (drop required `GEMINI_API_KEY`; add `GCP_PROJECT`, `VERTEX_LOCATION`)
- Test: `services/agent/tests/test_settings.py`, `services/agent/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing settings test** — assert `GEMINI_API_KEY` is no longer required, and `GCP_PROJECT` + `VERTEX_LOCATION` load (with a sensible `VERTEX_LOCATION` default).

```python
def test_settings_vertex_fields(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("CARTESIA_API_KEY", "k")
    monkeypatch.setenv("DEFAULT_CARTESIA_VOICE_ID", "v")
    monkeypatch.setenv("GCP_PROJECT", "usan-retirement")
    s = Settings()
    assert s.gcp_project == "usan-retirement"
    assert s.vertex_location  # has a default
```

- [ ] **Step 2: Run it — expect FAIL** (`gcp_project` field missing). `cd services/agent && uv run pytest tests/test_settings.py -k vertex -v`
- [ ] **Step 3: Update `settings.py`** — remove the required `gemini_api_key` field; add:

```python
gcp_project: str = Field(..., min_length=1, alias="GCP_PROJECT")
vertex_location: str = Field(default="global", alias="VERTEX_LOCATION")
```

- [ ] **Step 4: Run it — expect PASS.**
- [ ] **Step 5: Update the pipeline** — `pipeline.py`:

```python
llm=google.LLM(
    model=LLM_MODEL,
    vertexai=True,
    project=settings.gcp_project,
    location=settings.vertex_location,
),  # no api_key → plugin uses ADC (attached VM service account, cloud-platform scope)
```

- [ ] **Step 6: Update/adjust `test_pipeline.py`** to assert the session builds with the Vertex wiring (mock `google.LLM` and assert it is called with `vertexai=True, project=…`). Run the agent test suite; keep coverage ≥ 80%.
- [ ] **Step 7: Update env templates** — `.env.example`, compose env passthrough (`GCP_PROJECT`, `VERTEX_LOCATION`; drop `GEMINI_API_KEY`), and the prod secret notes. Commit.

> **Decision (D1):** auth = **attached-SA ADC** (recommended — no key file on disk) vs a service-account key file. ADC requires the VM's service account to have `roles/aiplatform.user` and `aiplatform.googleapis.com` enabled (Task A2).
> **Decision (D2) — RESOLVED:** `gemini-3.1-flash-lite` is served on Vertex **only via the `global` endpoint** (verified: 200 on `location=global`, 404 on regional `us-east1`/`us-central1`). So `VERTEX_LOCATION=global` and the model is **kept** (no downgrade). Trade-off to note: the `global` endpoint lets Google route the request to any region, so it does **not** pin data residency to the US (still BAA-covered — residency ≠ compliance). If strict US residency is required, the alternative is a regional endpoint (`us-east1`) with a regionally-served model (`gemini-2.5-flash`/`-flash-lite`).

### Task A2: Vertex IAM + API enablement (Terraform)

**Files:** Modify `infra/terraform/main.tf` (or a new `vertex.tf`)

- [ ] Enable `aiplatform.googleapis.com` (`google_project_service`).
- [ ] Grant the attached VM service account `roles/aiplatform.user`.
- [ ] `terraform plan` → review (additive only) → `terraform apply`.
- [ ] Rotate the secret/.env: remove `GEMINI_API_KEY`, add `GCP_PROJECT=usan-retirement` + `VERTEX_LOCATION=<chosen>`.

### Task A3 (legal/procurement): Cartesia BAA + Zero Data Retention — GATE

- [ ] Contact Cartesia sales; execute a **signed HIPAA BAA** on an Enterprise account (do not rely on the marketing HIPAA badge).
- [ ] Move the in-use `CARTESIA_API_KEY` onto that account; **enable Zero Data Retention** (opt-in per Cartesia DPA §13 — OFF by default).
- [ ] Get written confirmation the BAA + ZDR cover **both** the `ink-whisper` STT path (raw audio) **and** the TTS path (transcript text) under the single key.
- [ ] **Fallback (only if BAA/ZDR/pricing unacceptable):** swap STT→Deepgram (`livekit-plugins-deepgram`, BAA on request) + TTS→Google Cloud TTS (`livekit-plugins-google`, already a dep, folds under the existing Google Cloud BAA). Isolated edit in `pipeline.py:48-59` + `settings.py` + tests; re-tune latency for elderly callers. **Decision (D3).**

### Task A4 (legal/procurement): Confirm Google Cloud BAA scope — GATE

- [ ] Confirm the **signed** BAA version for project `usan-retirement` covers: Generative AI on **Gemini Enterprise Agent Platform** (formerly Vertex AI) for the chosen model+region; **Cloud Logging + Cloud Monitoring**; **Cloud SQL**; **Cloud Storage**.
- [ ] Confirm Vertex HIPAA data governance: no training on prompts/responses; abuse logging disabled or covered. (Pre-GA offerings are excluded from the BAA — verify GA status of the model.)

### Task A5 (legal/procurement): Telnyx BAA — GATE

- [ ] Email sales@telnyx.com; execute the Telnyx BAA scoped to Programmatic Voice / SIP trunking; state Telnyx recording + Telnyx STT are NOT used (recording via LiveKit egress to our GCS; STT via Cartesia).
- [ ] Verify Telnyx-side call recording stays **disabled** on the trunk; confirm **SRTP** media encryption on the trunk (signaling TLS alone does not protect the audio payload).
- [ ] Get the conduit-exception-vs-BAA position in writing. **Decision (D4):** execute belt-and-suspenders (recommended) vs rely on conduit exception with written confirmation.

---

## Workstream B — Observability + PHI audit-log durability (HIPAA §164.312(b))

The PHI-access audit events (`calls.py:205` "Recording URL accessed", `calls.py:234` "Transcript accessed") currently live only in ephemeral container stdout. Events are content-free (good) but there is no durable, queryable, retained audit trail (a required control).

**Files:** `apps/api/src/usan_api/logging_config.py`, `services/agent/src/usan_agent/logging_config.py`, `infra/docker-compose.prod.yml`, `infra/terraform/startup.sh`, `infra/terraform/*.tf`, `infra/terraform/variables.tf`

- [ ] **B1 (code):** make loguru emit JSON (`serialize=True`) + map level→severity in **both** `logging_config.py` files, so Cloud Logging indexes structured fields. Keep audit events content-free. TDD: assert the audit log record carries `call_id`/`client`/`segments` and no content.
- [ ] **B2 (infra):** route container stdout to journald (per-service `logging: { driver: journald }` in `docker-compose.prod.yml`, or `daemon.json` in `startup.sh` — Debian `json-file` does not reach journald).
- [ ] **B3 (infra):** idempotently install + configure the **Google Cloud Ops Agent** in `startup.sh` (`add-google-cloud-ops-agent-repo.sh --also-install`; `config.yaml` = `systemd_journald` receiver + `parse_json` processor + default hostmetrics). Reject the `docker gcplogs` driver (unmaintained) and a Fluent Bit container (over-engineered for one VM).
- [ ] **B4 (infra):** Terraform `google_logging_project_bucket_config` to raise audit retention (and/or a dedicated, optionally-locked bucket + `google_logging_project_sink` filtered to the audit lines) for an immutable trail. Add var `audit_log_retention_days`. **Decision (D5):** retention window + dedicated locked bucket now vs raise `_Default` for v1.
- [ ] **B5 (infra):** Terraform `google_monitoring_notification_channel` (operator email) + `google_monitoring_alert_policy` for CPU/mem/disk > ~85% and a log-based-metric container-down/Docker-restart alert. Add var `operator_alert_email`. **Decision (D6):** alert target (email / PagerDuty). (IAM `logWriter`+`metricWriter` already granted — `main.tf:72-82`.)

---

## Workstream C — Media-plane networking hardening (perf/capacity, NOT a PHI gate)

**Files:** `infra/docker-compose.prod.yml`, env/config for `LIVEKIT_URL`, egress `ws_url`, livekit webhook URL

- [x] **C1 (done, PR pending):** added `network_mode: host` to **livekit** and **livekit-sip** in the prod overlay; cleared their published `ports:` (`!reset null`) — removes the docker-proxy fan-out that wedged the VM and blocks downsizing. Dev base compose stays on bridge + narrow ranges (Docker-Desktop-on-Mac safety).
- [x] **C2 (done):** widened media ranges to LiveKit defaults to match the already-open GCP firewall (rtc `50000-60000`, rtp `10000-20000`); kept `use_external_ip: true`.
- [x] **C3 (done — corrected addressing):** host mode breaks compose DNS, so repointed each hop by *which network the client is on*, NOT a blanket loopback:
  - **bridge → host** (`api`, `agent`, `egress`, `caddy` reaching livekit): `ws://host.docker.internal:7880` + `extra_hosts: ["host.docker.internal:host-gateway"]`. The plan's original "`ws://127.0.0.1:7880` for api/agent" was wrong — from a *bridge* container `127.0.0.1` is the container's own loopback, not the host, so it would silently fail.
  - **host → host** (`livekit-sip` → livekit): `ws://127.0.0.1:7880` (both on host).
  - **host → bridge-published** (`livekit`/`livekit-sip` → redis, livekit webhook → api): `127.0.0.1:6379` / `http://127.0.0.1:8000/webhooks/livekit` (redis + api publish on the host loopback).
  - `egress` → redis stays `redis:6379` (both on bridge). Verified with `docker compose config` (renders clean).
- [x] **C4 (deterministic checks done on VM @ v0.1.5; live-call test pending):** verified on `usan-vm` 2026-06-05:
  - All containers on `v0.1.5`; livekit + livekit-sip `NetworkMode=host`; api/agent/egress/caddy on the bridge.
  - **No docker-proxy for media ports** — only caddy(80/443), redis(127.0.0.1:6379), api(127.0.0.1:8000) remain. The fan-out is gone.
  - livekit binds `*:7880`/`*:7881`; livekit-sip binds `*:5060`; ICE range live `[50000,60000]`; nodeIP `34.26.133.111`.
  - livekit→redis + livekit-sip→redis on `127.0.0.1:6379`; livekit-sip `local:10.142.0.2 external:34.26.133.111` (real NIC, host-mode win).
  - **agent registered at `ws://host.docker.internal:7880`** — bridge→host hop proven live (validates the C3 correction).
  - host→api webhook `127.0.0.1:8000/health → 200`; api log clean.
  - livekit STUN-resolved the external IP and advertises `["34.26.133.111/10.142.0.2", bridge-gw...]`; the boot-time `could not validate external IP / context canceled` warn is benign.
  - **STILL PENDING (needs a human to place/answer a real call):** end-to-end two-way call audio + egress upload to GCS + a live livekit→api webhook on a real room. Everything those depend on is independently confirmed reachable, but the audio path itself is unverified.
- [ ] **C5 (separate, measured follow-up):** only after C1–C4, measure agent model pre-warm (silero VAD + turn-detector) + egress GStreamer CPU/RAM, then consider `e2-standard-2 → e2-medium`. **Host mode is a prerequisite for downsizing, not a justification.** **Decision (D7):** attempt downsize vs defer.

> Alternative (D8): SFU-only `rtc.udp_port:7882` single-port mux (keep SFU on bridge, one docker-proxy proc, firewall to udp/7882) — but livekit-sip rtp has no mux, so SIP still needs host mode or a narrow range. Recommendation: host networking for both.

---

## Workstream D — Cloudflare token (CLOSED 2026-06-05)

- [x] **Premise corrected:** the token was **never committed**. Verified across all
  history — no tracked `terraform.tfvars` (it's gitignored), no commit ever held a real
  value, gitleaks passed on every PR. `dd7a5b9` (#23) only added the variable declaration,
  `var.`-references in `dns.tf`, and a commented placeholder in `.example`. The real value
  always lived only in the gitignored on-disk `infra/terraform/terraform.tfvars` — correct
  handling. The original "committed secret" framing here was inaccurate.
- [x] **Rotated anyway:** the live token was accidentally printed to the assistant session
  transcript during this investigation (a masking command failed), so it was rotated at
  dash.cloudflare.com out of caution. Done per user 2026-06-05.
- Optional future hardening (NOT required): source `cloudflare_api_token` from a
  `TF_VAR_cloudflare_api_token` env var backed by Secret Manager at apply time, to drop the
  on-disk plaintext footprint. Deferred.

---

## Workstream E — Container registry → Artifact Registry + keyless supply chain (security hygiene)

Images live in GHCR today (`ghcr.io/${GHCR_OWNER}/usan-{api,agent}`). Push uses the free `GITHUB_TOKEN` (fine), but the **VM pull needs a long-lived `GHCR_PAT`** (`docker login` on the box + a GitHub secret). Images are app code, **not PHI** — so this is operational hardening, not a compliance gate. The wins: kill `GHCR_PAT` (VM pulls keyless via its own service account, like the Vertex ADC pattern), keep the runtime supply chain in-region inside the GCP boundary, and get free vulnerability scanning.

**Files:** `infra/terraform/*.tf` (+ `variables.tf`), `.github/workflows/build.yml`, `infra/docker-compose.prod.yml`, `infra/terraform/startup.sh`, `infra/README.md`

- [ ] **E1 (infra):** Terraform `google_artifact_registry_repository` (Docker, `us-east1`, e.g. `us-east1-docker.pkg.dev/usan-retirement/usan`) + enable `artifactregistry.googleapis.com`. Grant the **VM service account** `roles/artifactregistry.reader`.
- [ ] **E2 (infra):** Terraform a **Workload Identity Federation** pool/provider for GitHub Actions + a deploy SA with `roles/artifactregistry.writer`, scoped to this repo (`attribute.repository`). Keyless — **no SA key in GitHub secrets**. Add vars for the GitHub repo/owner.
- [ ] **E3 (CI):** `build.yml` — replace the GHCR `docker/login-action` with `google-github-actions/auth` (WIF) + `gcloud auth configure-docker us-east1-docker.pkg.dev`; push images to the GAR path. (`GITHUB_TOKEN` GHCR login can stay or be removed.)
- [ ] **E4 (VM/compose):** configure the GAR cred helper for the VM SA in `startup.sh` (`gcloud auth configure-docker us-east1-docker.pkg.dev -q`); flip `docker-compose.prod.yml` image refs `ghcr.io/...` → `us-east1-docker.pkg.dev/usan-retirement/usan/usan-{api,agent}`; **remove** the `GHCR_PAT` `docker login` step from the deploy job.
- [ ] **E5 (cleanup):** delete the `GHCR_PAT` GitHub secret and **revoke the PAT**; update `infra/README.md`. Optionally enable Artifact Analysis (vuln scanning) on the repo.

> **Cutover ordering:** apply E1 (repo + VM-SA reader) and push at least one image tag to GAR **before** flipping the compose refs — otherwise the VM can't pull and the deploy fails. Do this as its own PR/tag, not stacked with another infra change in flight.

> **Decisions (D9, D10):** D9 — move to GAR (recommended) vs stay on GHCR. D10 — CI→GCP auth via Workload Identity Federation (recommended, keyless) vs a service-account key in GitHub secrets.

---

## Decisions for the user (blocking the relevant tasks)

- **D1** LLM auth: attached-SA ADC (recommended) vs key file.
- **D2 (RESOLVED)** Keep `gemini-3.1-flash-lite` on `VERTEX_LOCATION=global` (the only endpoint that serves it). Open sub-decision only if strict US data residency is required: switch to a regional endpoint + regionally-served model.
- **D3** Cartesia: KEEP under Enterprise BAA + ZDR (zero code) vs SWAP to Deepgram + Google Cloud TTS (fewer BAAs, reuses Google BAA).
- **D4** Telnyx: execute BAA belt-and-suspenders (recommended) vs conduit-exception-only.
- **D5** Audit-log retention window + dedicated locked bucket now vs later.
- **D6** Operator alert target (email/PagerDuty).
- **D7** VM downsize: measured follow-up vs defer.
- **D8** Media: host networking for both (recommended) vs SFU-only udp mux.
- **D9** Registry: move images to GCP Artifact Registry (recommended — keyless VM pulls, kills `GHCR_PAT`, in-region, vuln scanning) vs stay on GHCR.
- **D10** CI→GCP auth: Workload Identity Federation (recommended — keyless) vs a service-account key in GitHub secrets.

## Top risks

- **Active CRITICAL:** every staging call with realistic data is a PHI exposure until A1 ships AND A4 confirms scope — gate staging on it.
- **BAA-scope mismatch:** coverage is point-in-time and model/region/feature-specific; all Google/Cartesia/Telnyx confirmations must close before go-live, not be assumed.
- **Cartesia compliance is config-dependent:** ZDR is OFF by default; the current key is presumed not under a BAA.
- **Host-networking cutover** can silently break the control plane (egress ws_url + livekit→api webhook) — validate on stage (C4).
- **VM-wedge regression:** widening media ranges is only safe AFTER docker-proxy is removed via host mode.
- **SIP media may be plain RTP** unless SRTP is enforced on the Telnyx↔livekit-sip trunk.
- **Registry cutover:** flipping compose to GAR before the VM SA has `artifactregistry.reader` (E1) or before the tag is pushed to GAR (E3) leaves the VM unable to pull — ship E as its own PR/tag, reader-grant first.

## Out of scope (tracked elsewhere)

- **Daily-call scheduler** — no batch trigger exists in repo; `enqueue_call` is external-only. Must be resolved before the RetellAI cutover (separate plan).
- **Inbound calling** — config files exist; wiring is a separate track.

## Execution order

1. **A1 + A2** (Vertex migration — the active CRITICAL, ours to fix) → its own PR + `v*` tag.
2. **B** (audit durability) → PR + tag.
3. **A3/A4/A5** (legal BAAs — run in parallel from day 1; they gate go-live, not code).
4. **C** (media hardening) → PR + tag; **D** (token rotation) + **E** (registry → GAR/WIF) alongside — each its own PR (D and E both kill a long-lived secret; don't stack E with another in-flight infra change).
5. **C5** VM downsize as a measured follow-up.
