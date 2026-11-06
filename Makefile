COMPOSE := docker compose --profile collab --profile gateway

.PHONY: help switch-env env up down ps logs hello test seed invoices

help:
	@echo "make switch-env  generate switch/stack/.env (Switch server secrets)"
	@echo "make env         create .env from .env.example, copying the Switch tokens"
	@echo "make up          start Switch (core, Mattermost, Gateway) and Khata's database"
	@echo "make down        stop everything (data volumes are kept)"
	@echo "make seed        rebuild Khata's database and generate the invoice PDFs + golden set"
	@echo "make hello       run the Phase 0 smoke-test agent"
	@echo "make test        run the test suite"

switch-env:
	./scripts/switch_env.sh

env: switch-env
	@test -f .env || cp .env.example .env
	@tok=$$(grep '^AGENT_REGISTRATION_TOKEN=' switch/stack/.env | cut -d= -f2); \
	  sed -i.bak "s|^SWITCH_REGISTRATION_TOKEN=.*|SWITCH_REGISTRATION_TOKEN=$$tok|" .env
	@pw=$$(grep '^GATEWAY_ADMIN_PASSWORD=' switch/stack/.env | cut -d= -f2); \
	  sed -i.bak "s|^SWITCH_GATEWAY_PASSWORD=.*|SWITCH_GATEWAY_PASSWORD=$$pw|" .env
	@rm -f .env.bak
	@echo ".env ready - add GROQ_API_KEY / OPENROUTER_API_KEY and model names"

up: switch-env
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps -a

logs:
	$(COMPOSE) logs -f switch setup

seed:
	uv run python -m db.seed
	uv run python -m data.generate_invoices

invoices:
	uv run python -m data.generate_invoices

hello:
	uv run python scripts/hello_agent.py

test:
	uv run pytest -q
