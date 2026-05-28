.PHONY: help up down logs base build rebuild ps

COMPOSE    := docker compose --env-file infra/.env -f infra/docker-compose.yml
BASE_IMAGE := usan-agent-base:local
AGENT_DIR  := services/agent

help:
	@echo "Targets:"
	@echo "  make up       Build base image if missing, then compose up -d"
	@echo "  make down     Stop and remove containers"
	@echo "  make logs     Tail logs from all services"
	@echo "  make ps       List running containers"
	@echo "  make base     Force rebuild of $(BASE_IMAGE) (model pre-warm)"
	@echo "  make build    Build all compose images (assumes base exists)"
	@echo "  make rebuild  Rebuild base + compose images from scratch"

up:
	@if ! docker image inspect $(BASE_IMAGE) >/dev/null 2>&1; then \
		echo "==> $(BASE_IMAGE) not found, building..."; \
		$(MAKE) base; \
	fi
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

base:
	docker build -f $(AGENT_DIR)/Dockerfile.base -t $(BASE_IMAGE) $(AGENT_DIR)

build:
	$(COMPOSE) build

rebuild: base build
