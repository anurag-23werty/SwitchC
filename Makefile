COMPOSE := docker compose --profile collab --profile gateway

.PHONY: backup setup-demo migrate help switch-env env up down ps logs hello test seed invoices mcp demo pending graph setup-switch serve eval dashboard steward-demo image

help:
	@echo "make switch-env  generate switch/stack/.env (Switch server secrets)"
	@echo "make env         create .env from .env.example, copying the Switch tokens"
	@echo "make up          start Switch (core, Mattermost, Gateway) and Khata's database"
	@echo "make down        stop everything (data volumes are kept)"
	@echo "make migrate     create or upgrade Khata's database schema (keeps your data)"
	@echo "make seed        DEMO ONLY: wipe Khata's database, load the demo business + invoice PDFs"
	@echo "make hello       run the Phase 0 smoke-test agent"
	@echo "make mcp AGENT=analyst   run the MCP tool server for one agent (stdio)"
	@echo "make setup-switch  agents, your team's accounts and rooms on Switch (from Khata's database)"
	@echo "make setup-demo    the same for the demo team (one shared password in .env)"
	@echo "make serve       run Khata's agents in the rooms (Ctrl-C to stop)"
	@echo "make demo        run D01-D09 through the agent workflow (live LLMs, no Switch)"
	@echo "make pending     invoices paused at the approval gate"
	@echo "make graph       print the workflow graph (Mermaid)"
	@echo "make eval        all 31 labelled invoices through the workflow -> evals/report.md"
	@echo "make dashboard   read-only ledger at http://127.0.0.1:8088"
	@echo "make steward-demo  D03, D04 -> rule proposal -> owner approves -> S01 auto-approved"
	@echo "make backup      dump Khata's and Switch's databases + payment files to backups/"
	@echo "make image       build the Khata container image (deploy/ for the VM setup)"
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

migrate:
	uv run khata db migrate

seed:
	uv run python -m db.seed
	uv run python -m data.generate_invoices

invoices:
	uv run python -m data.generate_invoices

hello:
	uv run python scripts/hello_agent.py

mcp:
	uv run python -m mcp_server.server --agent $(AGENT)

setup-switch:
	uv run khata setup-switch

setup-demo:
	uv run khata setup-switch --demo

serve:
	uv run khata -v serve

demo:
	uv run khata demo

pending:
	uv run khata pending

graph:
	uv run python -m khata.graph.diagram

eval:
	uv run python -m evals.run

dashboard:
	uv run khata dashboard

steward-demo:
	uv run khata steward-demo

image:
	docker build -t khata:local .

backup:
	./deploy/backup.sh

test:
	uv run pytest -q
