# Deploying Khata on one VM (Phase 6)

Everything runs from one compose file pair: Switch (core, Gateway, Mattermost), Khata's
Postgres, Khata's agents (`khata serve`), the read-only ledger (`khata dashboard`), and
Caddy for HTTPS. Only Caddy publishes ports (80, 443); everything else is on the
internal Docker network or bound to 127.0.0.1.

```
            https://chat.<domain>    -> Mattermost  (#ap-desk, #ap-audit, #ap-inbox)
 Caddy ---> https://switch.<domain>  -> Switch Gateway (operator dashboard, Telegram setup)
            https://ledger.<domain>  -> Khata dashboard (read-only, token required)
                                         khata-serve <-> switch:8000 (Agent Protocol)
                                         khata-serve  -> Groq / OpenRouter (HTTPS out)
                                         Telegram bridge: long polling, no ingress
```

## Steps

On a VM with Docker (4 GB RAM is enough for the demo; Mattermost wants ~1 GB):

```sh
git clone <repo> khata && cd khata
make env            # creates .env and switch/stack/.env with fresh secrets
# in .env: GROQ_API_KEY, OPENROUTER_API_KEY, model names, and
#   KHATA_DOMAIN=<ip with dashes>.sslip.io   (or your own domain)
#   ACME_EMAIL=you@example.com
#   KHATA_DASHBOARD_TOKEN=<long random string>
docker compose -f docker-compose.yml -f deploy/docker-compose.deploy.yml \
  --profile collab --profile gateway --profile deploy up -d --build
```

Then onboard your business inside the container (the schema is migrated automatically
when `khata-serve` starts). Put your `khata.yaml` and CSVs in `./config/` (gitignored;
see [self-hosting.md](self-hosting.md)), then:

```sh
K="docker compose -f docker-compose.yml -f deploy/docker-compose.deploy.yml --profile deploy"
$K run --rm khata-serve khata init /app/config/khata.yaml --as <owner>
$K run --rm khata-serve khata import vendors /app/config/vendors.csv --as <finance manager>
$K run --rm khata-serve khata setup-switch      # prints one-time passwords for new accounts
$K restart khata-serve
```

To try the demo business instead, use `khata-serve sh -c "python -m db.seed && khata
setup-switch --demo"` (this wipes Khata's database).

Daily backups: `COMPOSE="$K" ./deploy/backup.sh` from cron (see self-hosting.md).

`<ip>.sslip.io` names resolve to the IP inside them, so Let's Encrypt works without
buying a domain. Open ports 80 and 443 in the VM's firewall.

The ledger: `https://ledger.<domain>/?token=<KHATA_DASHBOARD_TOKEN>`.

## Tracing (optional)

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (Langfuse Cloud
or self-hosted) in `.env`. Each invoice run becomes one trace: every LangGraph node is
a span and every LLM call a generation (model, provider, cache hit, attempts, invoice
id), grouped by the run's thread. Prompts and outputs are sent only with
`LANGFUSE_CAPTURE_CONTENT=1`, since invoices are financial data. Postgres remains the
record either way (`llm_calls`, `tool_calls`, `audit_log`, all visible in the ledger).

## What was verified, and what wasn't

Verified on the local stack:
- the image builds (`docker build -t khata:local .`), with the embedding model baked in;
- the containerised `khata serve` connected all nine agents to Switch over the Docker
  network, **resumed an invoice run that had stopped mid-way** in the host process,
  took Priya's approval from Mattermost and uploaded the NEFT draft into the thread;
- the containerised dashboard refuses requests without the token (401);
- Caddy with this Caddyfile (`caddy validate`, and HTTPS on all three hostnames with
  Caddy's internal CA in place of Let's Encrypt): Mattermost 200, Gateway 200,
  ledger 401 without the token and 200 with it;
- tracing: `tests/test_tracing.py` runs real invoices through the graph with the
  Langfuse SDK and an in-memory OpenTelemetry exporter (one trace per run, nodes and
  generations inside it, no document text unless capture is on).

Also verified (later): the image's `khata db migrate`, `khata init` and `khata import`
against a fresh database with the config mounted at `/app/config`, and Tesseract OCR
inside the image reading a scanned invoice.

Not done: an actual VM. Free VM tiers (Oracle, GCP, AWS) all ask for a card, so this
was left for you to decide. Sending traces to a real Langfuse server was not tested
either (a local Langfuse stack wasn't started here).

## Gotcha found doing this

`docker run --env-file .env` keeps trailing spaces that python-dotenv strips, so
`GROQ_MODEL=openai/gpt-oss-120b ` became a model that doesn't exist (HTTP 404). Khata's
settings now strip whitespace from every value (`tests/test_config.py`).
