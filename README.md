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

docker compose --env-file infra/.env -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml logs -f
```

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
