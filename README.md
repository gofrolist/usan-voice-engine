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
