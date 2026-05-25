# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project

Self-hosted voice assistant for USAN Retirement daily wellness check-in calls.
Bidirectional telephony (inbound + outbound) via Telnyx + LiveKit. Replaces RetellAI.

Design spec: `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md`

## Layout

- `apps/api` — FastAPI (Python 3.14, uv). REST + tool endpoints + webhooks.
- `services/agent` — LiveKit Agents 1.x worker (Python 3.12, uv). The voice pipeline.
- `infra/` — Docker Compose + Terraform.

`apps/api` and `services/agent` do not import from each other.

## Commands

### API
```bash
cd apps/api && uv sync
uv run pytest -v
ruff check . && ruff format .
```

### Agent
```bash
cd services/agent && uv sync
uv run pytest -v
ruff check . && ruff format .
```

### Stack
```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d
```

## Conventions

- Commit format: `type(scope): description` — scopes: `api`, `agent`, `infra`, `ci`, `docs`.
- Python: type hints required, Pydantic for settings, loguru with lazy `{name}` placeholders.
- Docker: multi-stage, non-root UID 1001 `appuser`, BuildKit cache mounts.
- Env validated at startup.
- ruff: line-length 100, target py312 (agent) / py314 (api).
