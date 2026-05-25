# USAN Voice Engine — Design Spec

**Date:** 2026-05-25
**Status:** Draft for review
**Author:** Evgenii Vasilenko (with Claude Code)

## 1. Purpose & context

USAN Retirement provides daily wellness check-in calls for elders: medication reminders, mood/pain check-ins, family alerts on missed wellness signals. The product currently runs on RetellAI; this project replaces that platform with a self-hosted stack to cut per-minute cost at the current volume (5,000–50,000 calls/month) without losing the load-bearing features.

**Bar for replacement:** feature parity with the current Retell flows + room to evolve. Not minimum viable — we are migrating production traffic, not exploring a hypothesis.

**Non-goals (v1):** human transfer, post-call structured-data webhooks, multi-language, multi-tenant authn, HIPAA certification (we design for readiness — see §10).

## 2. Stack

| Layer | Choice | Rationale |
|---|---|---|
| Telephony | Telnyx SIP trunk | ~$0.008/min blended US, strong DTMF, LiveKit-compatible |
| Media plane | Self-hosted LiveKit SFU + livekit-sip | Flat infra cost ~$50–150/mo handles thousands of concurrent rooms; matches blueprint pattern |
| Voice runtime | LiveKit Agents 1.x (Python) | Native turn detector, DTMF, room lifecycle; replaces Retell's hosted runtime |
| STT | Cartesia Ink-Whisper (streaming) | Single-vendor with TTS; tuned for telephony 8kHz |
| LLM | Gemini 3.1 Flash Lite | Cheap, fast, function-calling supported |
| TTS | Cartesia Sonic (streaming) | Warm voice for elder UX; "continuations" reduce per-turn latency |
| Backend | FastAPI (Python 3.14, uv) | Mirrors blueprint `apps/api`; REST + tool endpoints + webhooks |
| Datastore | Postgres 16 + pgvector | Single store for relational + RAG embeddings |
| Object storage | S3-compatible (Backblaze B2 or AWS S3) | Recordings; cheap egress for replay/family review |
| Deploy | Docker Compose + Terraform on a single VM | Same pattern as `project-800ms` blueprint |

## 3. System architecture

Three deployable units mirror the `project-800ms` blueprint shape:

```
                       Telnyx (PSTN SIP trunk)
                        │           ▲
                        │ inbound   │ outbound
                        ▼           │
              ┌──────────────────────────────┐
              │   LiveKit SIP server         │
              │   (livekit-sip, self-host)   │
              └──────────────┬───────────────┘
                             │ WebRTC
                             ▼
                  ┌──────────────────────┐
                  │  LiveKit SFU :7880   │
                  └──┬──────────────┬────┘
                     │              │
            audio frames        room events
                     │              │
                     ▼              ▼
       ┌────────────────────────────────────┐
       │  services/agent (LiveKit Agents 1.x worker)
       │  ├─ Cartesia Ink-Whisper (STT)
       │  ├─ Gemini 3.1 Flash Lite (LLM + tools)
       │  ├─ Cartesia Sonic (TTS)
       │  ├─ LiveKit turn-detector plugin
       │  ├─ DTMF capture (telephony plugin)
       │  ├─ Voicemail detector (Telnyx AMD + transcript heuristic)
       │  └─ Function-call clients → HTTPS → apps/api
       └────────────────┬───────────────────┘
                        │ HTTP
                        ▼
       ┌────────────────────────────────────┐
       │  apps/api (FastAPI)
       │  ├─ /v1/calls, /v1/elders, /v1/dnc, /v1/rag/documents
       │  ├─ Tool endpoints (log_wellness, get_today_meds, ...)
       │  ├─ Retry orchestrator (APScheduler in-process)
       │  ├─ RAG retrieval (pgvector)
       │  ├─ Recording + transcript webhook receivers
       └────────┬───────────────────────┬───┘
                ▼                       ▼
       ┌─────────────────┐    ┌────────────────────┐
       │ Postgres 16     │    │ S3-compatible      │
       │ + pgvector      │    │ (recordings)       │
       └─────────────────┘    └────────────────────┘
```

### Outbound flow

1. External system → `POST /v1/calls` with `{elder_id, dynamic_vars, idempotency_key}`.
2. API runs DNC check; creates `calls` row (status=`queued`).
3. API creates a LiveKit room, requests SIP outbound dial via livekit-sip (Telnyx as trunk), dispatches the agent worker.
4. Agent enables Telnyx AMD; once SIP 200 OK is received, first ~3s of transcript also runs through voicemail-pattern matcher.
5. **Voicemail branch:** play scripted leave-message via Cartesia → hangup → mark `voicemail_left` → schedule retry per policy.
6. **Human branch:** conversation loop (STT → LLM with tools → TTS), supporting DTMF and interruption; on hangup, LiveKit Egress writes recording to S3 and agent flushes transcript.

### Inbound flow

1. Telnyx routes incoming PSTN call to livekit-sip per trunk config.
2. LiveKit SIP creates a room via dispatch rule; agent worker is assigned.
3. Agent fires a webhook to `apps/api` for caller-ID → elder lookup; dynamic vars (name, today's meds, last call summary) are injected into the chat context.
4. Conversation loop runs (no voicemail detection on inbound).

## 4. Components

### 4.1 `apps/api` (FastAPI, Python 3.14, uv)

**Public REST surface:**

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/calls` | Enqueue outbound call. Body: `{elder_id, dynamic_vars, idempotency_key}`. Returns `call_id`, status URL. |
| GET | `/v1/calls/{call_id}` | Call status, presigned recording URL (1h TTL), transcript. |
| POST | `/v1/elders` / PUT `/v1/elders/{id}` | Elder records (name, phone, time zone, meds, family contacts). |
| POST | `/v1/dnc` / DELETE `/v1/dnc/{phone}` | DNC list management. |
| POST | `/v1/rag/documents` | Ingest a doc into pgvector (chunk + embed via Gemini text-embedding-004). |

**Tool endpoints** (JWT-authenticated; called by agent during a live call):

| Path | Purpose |
|---|---|
| `POST /v1/tools/log_wellness` | `{call_id, mood, pain_level, notes}` |
| `POST /v1/tools/log_medication` | `{call_id, med_id, taken, reported_time}` |
| `POST /v1/tools/get_today_meds` | `{elder_id}` → list of meds due |
| `POST /v1/tools/rag_search` | `{query}` → top-k chunks |
| `POST /v1/tools/end_call` | `{call_id, reason}` — graceful termination |

**Webhook receivers:**
- `POST /webhooks/livekit/egress` — recording-completed callback, updates `calls.recording_uri`.
- `POST /webhooks/livekit/room` — participant join/leave events for inbound caller-ID lookup.

**Internal services** (in-process, not separate processes):
- `RetryOrchestrator` (APScheduler) — re-dispatches voicemail/no-answer calls per policy with backoff and quiet-hour gating.
- `DNCChecker` — synchronous check on every outbound dispatch path.
- `LiveKitDispatcher` — creates room, mints JWT, kicks off SIP outbound, dispatches agent.

### 4.2 `services/agent` (LiveKit Agents 1.x, Python 3.12, uv)

A worker process registered with LiveKit. On dispatch, spawns a `VoicePipelineAgent`:

```python
agent = VoicePipelineAgent(
    vad=silero.VAD.load(),
    stt=cartesia.STT(model="ink-whisper"),
    llm=google.LLM(model="gemini-3.1-flash-lite", tools=[...]),
    tts=cartesia.TTS(voice=elder.preferred_voice or DEFAULT_VOICE),
    turn_detector=turn_detector.EOUModel(),
    chat_ctx=build_chat_ctx(dynamic_vars),
)
```

**Per-call modules:**
- `voicemail_detector.py` — fuses Telnyx AMD callback with first-3s transcript regex. On match: cancel in-flight LLM/TTS, play scripted leave-message, terminate.
- `dtmf_handler.py` — listens to LiveKit telephony events; exposes a digit buffer to the LLM as a tool (`get_dtmf_digits()`).
- `tools.py` — thin HTTPS clients to `apps/api` tool endpoints (httpx, short-lived JWT in `Authorization` header signed by the worker's per-call key).
- `lifecycle.py` — wires LiveKit room events (participant disconnect, network drop) to graceful shutdown and transcript flush.
- `reconnect.py` — on LiveKit client disconnect, hold room open 20s for SIP re-INVITE; otherwise end cleanly.

### 4.3 `infra/`

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base stack: `livekit`, `livekit-sip`, `api`, `agent`, `postgres` (with pgvector). |
| `docker-compose.prod.yml` | Prod overlay (GHCR images, healthchecks, restart policies). |
| `docker-compose.tls.yml` | Caddy TLS termination for API + LiveKit signaling. |
| `terraform/` | Single Hetzner CCX23 or AWS c7i.xlarge VM (no GPU — all models external). Secret loading at boot. |
| `.env.example` | `TELNYX_*`, `LIVEKIT_*`, `CARTESIA_API_KEY`, `GEMINI_API_KEY`, `DATABASE_URL`, `S3_*`, `JWT_SIGNING_KEY`. |

## 5. Data model

### 5.1 Schema (Postgres 16, migrations via Alembic)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE elders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT UNIQUE,
    name            TEXT NOT NULL,
    phone_e164      TEXT NOT NULL UNIQUE,
    timezone        TEXT NOT NULL,
    preferred_voice TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_elders_phone ON elders(phone_e164);

CREATE TABLE dnc_list (
    phone_e164  TEXT PRIMARY KEY,
    reason      TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TYPE call_direction AS ENUM ('outbound', 'inbound');
CREATE TYPE call_status AS ENUM (
    'queued', 'dialing', 'ringing', 'in_progress',
    'completed', 'voicemail_left', 'no_answer',
    'busy', 'failed', 'dnc_blocked', 'cancelled'
);

CREATE TABLE calls (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    elder_id           UUID REFERENCES elders(id) ON DELETE SET NULL,
    direction          call_direction NOT NULL,
    status             call_status NOT NULL DEFAULT 'queued',
    idempotency_key    TEXT UNIQUE,
    livekit_room       TEXT,
    sip_call_id        TEXT,
    dynamic_vars       JSONB NOT NULL DEFAULT '{}',
    parent_call_id     UUID REFERENCES calls(id),
    attempt            SMALLINT NOT NULL DEFAULT 1,
    scheduled_at       TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    answered_at        TIMESTAMPTZ,
    ended_at           TIMESTAMPTZ,
    duration_seconds   INTEGER,
    end_reason         TEXT,
    recording_uri      TEXT,
    error              JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_calls_elder ON calls(elder_id, created_at DESC);
CREATE INDEX idx_calls_status_scheduled ON calls(status, scheduled_at)
    WHERE status IN ('queued', 'no_answer', 'voicemail_left');
CREATE INDEX idx_calls_livekit_room ON calls(livekit_room);

CREATE TABLE transcripts (
    id          BIGSERIAL PRIMARY KEY,
    call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_name   TEXT,
    tool_args   JSONB,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transcripts_call ON transcripts(call_id, started_at);

CREATE TABLE wellness_logs (
    id            BIGSERIAL PRIMARY KEY,
    call_id       UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    elder_id      UUID NOT NULL REFERENCES elders(id),
    mood          SMALLINT,
    pain_level    SMALLINT,
    notes         TEXT,
    raw           JSONB NOT NULL DEFAULT '{}',
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE medication_logs (
    id              BIGSERIAL PRIMARY KEY,
    call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    elder_id        UUID NOT NULL REFERENCES elders(id),
    medication_name TEXT NOT NULL,
    taken           BOOLEAN NOT NULL,
    reported_time   TIMESTAMPTZ,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE rag_chunks (
    id          BIGSERIAL PRIMARY KEY,
    document_id UUID NOT NULL,
    title       TEXT,
    content     TEXT NOT NULL,
    embedding   vector(768) NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_rag_embedding ON rag_chunks USING hnsw (embedding vector_cosine_ops);
```

### 5.2 Call state machine

```
                  POST /v1/calls
                       │
              ┌────────▼────────┐
              │ DNC check       │
              └────┬────────┬───┘
                   │ pass   │ fail
                   ▼        ▼
                queued   dnc_blocked (terminal)
                   │
                   ▼
                dialing ────► failed (Telnyx error) ─► (retry once)
                   │
                   ▼
                ringing ───► no_answer ──► queued (attempt+1, +30min)
                   │                  └──► no_answer terminal at max_attempts
                   ▼
              in_progress
                   │
              ┌────┴───────────────────┐
              ▼                        ▼
       voicemail detected        human conversation
              │                        │
              ▼                        ▼
       leave message            user/agent hangup
              │                        │
              ▼                        ▼
       voicemail_left ─► (retry?)  completed (terminal)
```

### 5.3 Retry policy (v1 hardcoded)

| End state | Retry rule |
|---|---|
| `no_answer` | up to 2 more attempts: +30min, then +2h |
| `voicemail_left` | one more attempt after 3h, then stop |
| `busy` | one retry after 5min |
| `failed` (transport) | one retry after 1min |
| Any | never call before 9am or after 9pm in elder's local time zone |

### 5.4 Idempotency

`POST /v1/calls` requires an `idempotency_key`. `UNIQUE` on `calls.idempotency_key` makes the API safe for the external system to retry: a duplicate key returns `200 OK` with the original `call_id` and current status, rather than creating a new call or returning `409`. If the duplicate request's body differs from the original (different `elder_id` or `dynamic_vars`), the API returns `409 Conflict`.

## 6. End-of-turn latency

**Target:** p95 ≤ 1200ms (intentionally generous — elder UX favors warmth over speed).

```
User stops speaking
   │
   │  ~150ms  Silero VAD end-of-utterance
   │   ~50ms  LiveKit turn-detector semantic confirmation
   │  ~120ms  Cartesia Ink-Whisper final transcript
   │  ~400ms  Gemini 3.1 Flash Lite first token (cold) / ~150ms warm
   │   ~80ms  Cartesia Sonic first audio chunk
   ▼
First audio out: 800–1200ms
```

**Tuning levers** (apply in order if budget is missed):
1. Stream everything: STT partials → speculative LLM prefetch; LLM tokens → TTS chunks while generating.
2. Cartesia "continuations" — keep TTS websocket warm across turns.
3. Gemini context caching for the system prompt + RAG injection.
4. Pre-warm worker — agent holds Gemini client and Cartesia websocket open at startup.
5. Tool calls in parallel with TTS prefill — if a tool runs >300ms, play a brief "let me check" filler over it.

Add a deliberate ~250ms pause before the agent speaks: elder UX over raw latency.

## 7. Voicemail detection

Hybrid signal — either source triggers voicemail mode within 3s of human-answer event:

- **Telnyx AMD** callback (`machine_start` / `human_residence` / `unknown`).
- **Transcript regex** on first 3 seconds of STT output (case-insensitive):
  - `leave a (message|name)`
  - `you've reached`
  - `not available right now`
  - `after the (beep|tone)`

If signals conflict: transcript wins (more accurate on US English voicemail).

On match: cancel in-flight LLM/TTS, play scripted leave-message via Cartesia, hangup, status=`voicemail_left`.

## 8. Reconnection & error handling

| Scenario | Detection | Action |
|---|---|---|
| Caller drops mid-call | LiveKit `ParticipantDisconnected` | Hold room 20s; if no rejoin → mark `completed`, flush transcript, `end_reason=disconnect_no_recover` |
| Agent worker crash | LiveKit job heartbeat lost | LiveKit re-dispatches to another worker; resume turn loop from room metadata |
| Cartesia STT WS drop | onError | Reconnect with backoff (250ms, 500ms, 1s, 2s); buffer ≤2s audio frames; >3s total → end call `end_reason=stt_unavailable` |
| Cartesia TTS WS drop | onError | Same backoff; mid-utterance, do NOT replay — speak "one moment please" and resume |
| Gemini 5xx / timeout | httpx exception | One retry after 200ms; on failure → "let me try again" prompt; second failure → end with `error_apology` |
| Tool endpoint 5xx | httpx exception | Speak filler ("let me check on that later"), continue conversation |
| Telnyx SIP error | livekit-sip callback | `calls.status = failed`, retry per policy |
| Egress webhook missing | no callback within 5min of `ended_at` | `recording_uri = NULL`, log warning; call otherwise complete |
| Postgres unavailable on tool call | DB exception | Tool returns 503; agent says filler; transcripts buffer in worker memory and flush on reconnect |

## 9. Recording & transcript storage

- **Recording:** LiveKit Egress (composite room track) → S3-compatible bucket. Path `recordings/{YYYY-MM-DD}/{call_id}.ogg` (Opus mono ~24kbps). Lifecycle: cold storage after 30d, delete after configurable retention (default 1y).
- **Transcript:** agent emits segments via LiveKit data channel to `apps/api` debounced at 500ms; final flush on call end. Written to `transcripts` table.
- **Linkage:** `calls.recording_uri` (S3 path). `GET /v1/calls/{id}` returns a presigned URL (1h TTL, access logged).

## 10. Security & compliance

**HIPAA-readiness (not certification):**
- TLS 1.2+ everywhere (Caddy in front of API, LiveKit signaling, livekit-sip).
- Encryption at rest: Postgres disk encryption, S3 SSE-S3.
- Access logging: every read of recording/transcript audit-logged with caller identity.
- Secret management: env vars at boot, no secrets in code or images.
- Vendor BAAs targeted before production traffic: Telnyx, Cartesia (request), Google (Gemini), AWS/B2 (S3).
- Recording-consent disclosure: scripted in the opening greeting on both inbound and outbound; configurable per elder.

**Other:**
- TCPA quiet hours enforced at the retry orchestrator (9pm–9am local).
- DNC: synchronous check before any outbound dial; manual UI for adding.
- Service-to-service authn: JWT signed with `JWT_SIGNING_KEY` between agent → api.
- Webhook signatures verified on inbound LiveKit and Telnyx callbacks.

## 11. Observability

- **Logs:** loguru with structured fields (`call_id`, `elder_id`, `room`, `direction`, `attempt`). `docker logs` for v1; ship to Loki when traffic justifies it.
- **Metrics:** Prometheus exposition from both services. Key SLIs: end-of-turn p50/p95/p99, voicemail-detection accuracy (sampled and labelled weekly), call success rate, retry rate, Cartesia/Gemini error rate.
- **Tracing:** OpenTelemetry with W3C trace context propagated from `apps/api` → `services/agent` → tool calls. Instrument spans now; backend optional in v1.

## 12. Testing strategy

| Layer | Tooling | What we test |
|---|---|---|
| Unit | pytest, pytest-asyncio | handlers, retry math, DNC, idempotency, tool endpoints, voicemail regex, DTMF state, prompt templating |
| Integration | pytest + testcontainers | Real Postgres (pgvector + JSONB behaviour); API↔agent HTTP boundary; LiveKit clients stubbed |
| Audio/pipeline | Live fixtures, gated on `RUN_LIVE_TESTS=1` | Real Cartesia/Gemini against WAV fixtures of elder speech & voicemail greetings; latency assertions |
| Voicemail regression | Static fixture set (~30 voicemail + ~30 human) | Classification accuracy + latency on every PR |
| End-to-end smoke | Manual + `scripts/place_test_call.py` | Real Telnyx test number, scripted 3-step prompt; before each release |

**Coverage gates:** 80% lines / 80% branches per project rules; CI fails below.

## 13. Dev workflow & conventions

Mirrors the `project-800ms` blueprint:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d
# livekit, livekit-sip, postgres, api, agent

cd apps/api && uv sync && uv run pytest -v
cd services/agent && uv sync && uv run pytest -v
ruff check . && ruff format .

uv run python scripts/place_test_call.py +14155551234
```

- Commit format: `type(scope): description` — scopes: `api`, `agent`, `infra`, `ci`, `docs`.
- Python: type hints required, Pydantic for settings & validation, loguru lazy `{name}` placeholders.
- Docker: multi-stage, non-root UID 1001 `appuser`, BuildKit cache mounts. Agent image split `Dockerfile.base` (deps) + `Dockerfile` (code), same as blueprint.
- Pre-commit: ruff, gitleaks, actionlint, file hygiene.
- Env validated at startup via `require_env()`.

## 14. CI/CD

GitHub Actions:
- `lint.yml` — ruff on every PR.
- `test.yml` — pytest with Postgres service container on every PR.
- `build.yml` — multi-arch Docker builds (api + agent), push to GHCR on `main`.
- `deploy.yml` — on tag, `ssh` to VM, `docker compose pull && up -d`, post-deploy health check.

## 15. Out of scope for v1

- Human transfer (SIP REFER bridging).
- Post-call structured-data webhooks.
- Multi-language (English only).
- Multi-tenant authn.
- HIPAA certification (we design for readiness; certification follows BAAs).
- Real-time supervisor / barge-in dashboard.

## 16. Open questions / decisions to revisit

- **Object storage choice:** Backblaze B2 (cheap egress) vs AWS S3 (broader ecosystem). Resolve at infra setup.
- **VM provider:** Hetzner (cheapest) vs AWS (BAA easier for HIPAA). Resolve based on compliance timeline.
- **RAG ingestion UX:** API-only in v1; lightweight admin UI deferred.
- **Voice selection per elder:** default Cartesia voice in v1; per-elder voice override is in the schema but no UI yet.
