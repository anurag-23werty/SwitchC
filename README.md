# Khata: an AI accounts-payable desk for small businesses

Khata turns your finance team's chat into an accounts-payable desk. Post a vendor
invoice (a PDF, a scan or a phone photo) in the chat room, and Khata's agents read it,
check it, explain anything unusual, and route it to the right person. That person
replies `approve <reason>` or `reject <reason>`, and approved invoices become a NEFT
bulk-upload file and journal entries for you to upload to your bank.

It is built for Indian small businesses: GST-aware (GSTIN check digits, IGST vs
CGST + SGST, the split, the arithmetic), works with or without purchase orders, and runs
on your own machine on free AI tiers. It runs on [Switch](https://github.com/sandbox-quantum/switch),
which connects the agents to your team's chat (Mattermost out of the box; Slack, Teams,
Discord or Telegram through Switch's bridges).

## Why you can trust it with money

- **No agent pays.** Khata drafts a bank-upload file; a person uploads it.
- **No AI makes a money decision.** Every check (GST, arithmetic, duplicates, PO/GRN
  match, bank details) and every routing decision is plain code against your policy.
  The models only read the document and write explanations.
- **Two different models read every invoice**, and the amount, GSTIN, invoice number and
  bank account must match exactly and appear in the document, or a person looks.
- **Bank details are a hard stop.** An invoice naming a different account than your
  vendor master is held for the owner, who is told to call the vendor on the number you
  have on file. Bank details can't be changed from an invoice or a chat message.
- **Invoice text is untrusted.** Text aimed at AI agents is detected and the invoice is
  held.
- **Only the right person can approve**, a reason is required, and everything (every
  check, model call, tool call and decision) is in an append-only audit log.

## Try it in five commands (demo business)

Needs Docker, [uv](https://docs.astral.sh/uv/), `make`, and free
[Groq](https://console.groq.com/keys) and [OpenRouter](https://openrouter.ai/keys) keys in `.env`.

```sh
make env && make up       # Switch + Mattermost + Khata's database (add LLM keys to .env)
uv sync && make seed      # DEMO: a sample business, vendors, POs and 31 invoice PDFs
make setup-demo           # agents, demo team accounts, #ap-desk / #ap-audit / #ap-inbox
make serve                # the agents go to work
```

Open http://localhost:8065, sign in as `ravi` (password: `KHATA_DEMO_PASSWORD` in `.env`)
and drop `data/invoices/D01_…pdf` into **#ap-desk**. Sign in as `priya` or `owner` to
approve. Or without the chat: `make demo` runs the nine demo invoices in the terminal.

## Run it for your business

[docs/self-hosting.md](docs/self-hosting.md) is the full guide. In short:

```sh
make env && make up && uv sync && make migrate
mkdir config && cp examples/khata.yaml examples/vendors.csv config/   # fill them in
uv run khata init config/khata.yaml --as <owner>
uv run khata import vendors config/vendors.csv --as <finance manager>
uv run khata setup-switch && make serve
```

For a server your team reaches over HTTPS (Caddy, automatic certificates), see
[docs/deploy.md](docs/deploy.md). Back up with `make backup`.

## What it does

| | |
|---|---|
| **Reads** | PDFs; scans and photos through local OCR (always sent to a person) |
| **Checks** | GSTIN and GST type and split, arithmetic, duplicates and near-duplicates, vendor master, bank details, PO prices and GRN quantities (each optional) |
| **Routes** | By your policy: auto-approve small clean invoices, finance manager, then owner above a limit; holds for anything suspicious |
| **Explains** | An Analyst note for the approver citing past decisions for that vendor (pgvector search over your history), checked by an Auditor on a different model |
| **Learns** | The Policy Steward proposes a rule when people keep approving the same exception; only the owner can accept it, as a new policy version |
| **Talks to vendors** | `#ap-inbox` (Telegram-ready): vendors ask for status and send invoices; bank-change requests are refused and the owner alerted |
| **Pays (almost)** | NEFT bulk-upload CSV and journal entries, idempotent per invoice |
| **Shows** | A read-only ledger dashboard: queue, invoice history, audit trail, policy versions, metrics |

## How it works

```
 your team in #ap-desk ── Switch (Agent Protocol) ── Khata's agents ── Postgres (system of record)
                                                       │
  Dispatcher → Intake (2 models + OCR) → Verifier (checks) → Router (policy) → Analyst → Auditor
       → a person decides in the thread → Payer (NEFT draft) → Policy Steward
  Vendor Liaison in #ap-inbox · every step logged · LangGraph checkpoints survive restarts
```

Nine agents, each a Switch participant with its own name and only the tools it's
allowed: [docs/switch-room.md](docs/switch-room.md). The workflow and models:
[docs/agent-runtime.md](docs/agent-runtime.md). Checks, policy and permissions:
[docs/tools-and-policy.md](docs/tools-and-policy.md).

## How well it works

`make eval` runs 31 hand-labelled invoices through the real workflow and grades every
money decision by exact match ([evals/report.md](evals/report.md),
[docs/evals.md](docs/evals.md)):

| Fraud caught | Duplicates stopped | Injection resisted | Decisions as labelled | Critical fields read right |
|---|---|---|---|---|
| 100% (3/3) | 100% (3/3) | 100% (2/2) | 100% | 100% |

The set is small and its PDFs are clean and digital; treat it as a regression suite, not
a promise about every invoice you'll receive.

## Development

```sh
make up && uv sync && make seed
make test          # 340 tests: checks, policy, workflow, Switch service, onboarding, OCR
uv run ruff check . --exclude vendor
```

CI runs lint and the full suite against pgvector Postgres on every push
(`.github/workflows/ci.yml`). Schema changes go in a new numbered file in
`db/migrations/`.

## Roadmap

- Invoices by email (an inbox that posts into #ap-desk)
- Export to Tally / Zoho Books; bank-specific NEFT layouts
- Several businesses per installation
- Vendor onboarding with bank-account verification (penny drop)

## Background

Khata was started for the WeMakeDevs *Flip the Switch* hackathon and then turned into a
tool meant for real use. The Switch findings from building it are in
[docs/switch-notes.md](docs/switch-notes.md).
