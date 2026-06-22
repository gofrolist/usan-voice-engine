# usan-voice-engine

Self-hosted voice assistant for USAN Retirement daily wellness check-in calls.
Replaces RetellAI with a self-hosted LiveKit Agents 1.x + Cartesia + Gemini stack.

## Stack

- Media plane: self-hosted LiveKit SFU + livekit-sip
- Telephony: Telnyx SIP trunk
- STT: Cartesia Ink-Whisper
- LLM: Gemini 3.1 Flash Lite
- TTS: Cartesia Sonic
- Voice runtime: LiveKit Agents 1.x (Python)
- API: FastAPI (Python 3.14, uv)
- Storage: Postgres 16 + pgvector, S3-compatible for recordings

See `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md` for the full design.

## Bring-up

```bash
cp infra/.env.example infra/.env
# Fill in Telnyx, Cartesia, Gemini, LiveKit secrets

make up      # builds usan-agent-base:local on first run, then compose up -d
make logs
```

The agent uses a split Dockerfile: a heavy `Dockerfile.base` that pre-warms Silero
VAD + turn-detector models, and a thin `Dockerfile` that copies app code on top.
`make up` builds the base image on first run; rebuild it explicitly with `make base`
when `services/agent/Dockerfile.base`, `pyproject.toml`, or `uv.lock` changes.
Run `make help` for the full target list.

## Development

```bash
# API
cd apps/api && uv sync && uv run pytest -v

# Agent
cd services/agent && uv sync && uv run pytest -v

# Lint
ruff check . && ruff format .
pre-commit install
```

## RetellAI-compatible API

The engine exposes an **additive, RetellAI-compatible API surface** on the same base URL as
the native `/v1` API, so a CRM already built on [RetellAI](https://docs.retellai.com) migrates by
repointing its base URL + API key — no client rewrite. It is a mounted FastAPI sub-app under
`apps/api/src/usan_api/compat/`; the native `/v1` plane and `services/agent` are unchanged.

- **Auth** — a super-admin issues a per-org key on the native plane (`POST /v1/admin/compat-keys`,
  token shown once); the CRM sends it as `Authorization: Bearer key_…`. Errors use RetellAI's
  `{status, message}` envelope (never the native `{detail}`).
- **Calls** — `POST /v2/create-phone-call`, `GET /v2/get-call/{id}`, `POST /v3/list-calls`,
  `POST /v2/stop-call/{id}`, `PATCH /v2/update-call/{id}`. A brand-new `to_number` auto-creates a
  Contact; DNC / quiet-hours return an explicit `400`.
- **Webhooks** — `call_started` / `call_ended` / `call_analyzed` in the `{event, call}` shape with an
  `x-retell-signature` the CRM's `retell-sdk` verifies; full-fidelity (PHI) only to allow-listed hosts.
- **Agents & Retell-LLMs** — full CRUD + versioning/publish; one inventory with admin-UI agents.
- **Batch** — `POST /create-batch-call` (unversioned) launches gated, tracked outbound campaigns.
- **Lookups** — `list-voices` / `get-voice` / `get-concurrency`; every out-of-scope RetellAI endpoint
  returns a documented `501 not_supported`.

PHI never leaves the engine's BAA-covered pipeline: the RetellAI `model` / `model_temperature` are
echoed but not honored (the prompt runs on the engine's own Vertex pipeline). The compat OpenAPI is
separate from the native docs and gated by `COMPAT_DOCS_ENABLED`.

Design + contract: [`specs/003-retellai-api-parity/`](specs/003-retellai-api-parity/) (spec, plan,
data-model, `contracts/endpoints.md`). A handful of exact RetellAI shapes are marked **PENDING-FREEZE**
in the contract — pinned against the CRM's captured real traffic before the contract is frozen.
