# Running Khata for your business

This guide is for a small business (or the person who looks after its IT) that wants
Khata to handle its vendor invoices for real. Khata runs on your own machine or server;
your invoices and books never go to a Khata service, because there isn't one. Invoice
text is sent only to the LLM providers you configure (Groq and OpenRouter by default).

**What it does, day to day.** Your team posts vendor invoices (PDFs, scans or phone
photos) into a chat room. Khata's agents read each one with two different AI models,
check the GST, the arithmetic, duplicates, the vendor's bank details, and your purchase
orders and goods receipts if you use them, then route it under your approval policy:
small clean invoices are approved automatically, the rest go to the right person in the
room, who replies `approve <reason>` or `reject <reason>`. Approved invoices become a
NEFT bulk-upload file and journal entries for you to upload to your bank. **Khata never
pays anything and never changes a vendor's bank details.**

## What you need

| | |
|---|---|
| A machine | Linux VM, Mac or Windows with Docker; 4 GB RAM, 20 GB disk |
| Software | Docker (with Compose), `git`, [uv](https://docs.astral.sh/uv/), `make` |
| LLM keys | [Groq](https://console.groq.com/keys) and [OpenRouter](https://openrouter.ai/keys), both free without a card |
| Your data | Company GSTIN and bank account; your team; your vendors with GSTIN, phone and bank details |

**How many invoices the free tiers handle.** Each invoice uses 2 model calls to read it,
plus 2 more (explanation and its audit) when a person needs to decide. OpenRouter's free
tier allows 50 requests a day, so plan on roughly 20–25 invoices a day on free keys.
When a provider's daily limit is reached Khata falls back to the next model, and if none
is available the invoice waits safely and can be retried (`retry` in its thread). For more
volume, add credit to OpenRouter or point the model settings in `.env` at paid models;
nothing else changes.

## 1. Install

```sh
git clone <this repo> khata && cd khata
make env        # creates .env and Switch's secrets
```

Edit `.env`: add `GROQ_API_KEY`, `GROQ_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`
(see `.env.example`), and change `KHATA_DB_PASSWORD` from the default.

```sh
make up         # Switch, Mattermost (your team's chat), the Gateway, Khata's database
uv sync
make migrate    # creates Khata's database schema (safe to re-run; keeps data)
```

For a server reachable by your team over HTTPS, follow [deploy.md](deploy.md) instead of
`make up`; the onboarding steps below are the same, run inside the container.

## 2. Tell Khata about your business

Copy the examples and fill them in. Keep these files somewhere private: they contain
bank details.

```sh
mkdir config && cp examples/khata.yaml examples/*.csv config/
```

**`config/khata.yaml`**: your company, your team and your approval policy. Usernames
must match the usernames people will have in the chat. Roles: `owner` (approves big
invoices, bank changes and policy changes), `finance_manager` (approves most invoices,
manages vendors), `clerk` (posts invoices, records goods receipts). Set
`po_required: false` and `grn_required: false` if you don't raise purchase orders or
record goods receipts; Khata then checks invoices on GST, arithmetic, duplicates, bank
details and amount.

```sh
uv run khata init config/khata.yaml --as <owner username>
```

**`config/vendors.csv`**: one row per vendor. The phone number is the one Khata tells
the owner to call if an invoice ever names a different bank account, so use the number
you know is genuine, not one from an invoice.

```sh
uv run khata import vendors config/vendors.csv --as <finance manager>
uv run khata vendors
```

Every row is validated (GSTIN check digit, IFSC format, account number). Bad rows are
refused with a reason; good rows are imported. Re-running is safe.

**If you use purchase orders and goods receipts**, import them the same way (one row per
line; the formats are in `examples/pos.csv` and `examples/grns.csv`):

```sh
uv run khata import pos config/pos.csv --as <finance manager>
uv run khata import grns config/grns.csv --as <clerk>
```

## 3. Open the rooms

```sh
uv run khata setup-switch
```

This registers Khata's agents with Switch, creates a chat account for each person in
your config (if they don't have one), opens **#ap-desk** (invoices and approvals),
**#ap-audit** (the decision feed) and **#ap-inbox** (for vendors), and puts everyone in
the right rooms. New accounts are printed once with a one-time password: hand each one
over privately and ask people to change it when they first sign in.

Then start the agents and leave them running:

```sh
make serve           # or, on a server, the khata-serve container does this
make dashboard       # optional: a read-only ledger at http://127.0.0.1:8088
```

Open Mattermost (http://localhost:8065, or `https://chat.<your domain>`), sign in and
post an invoice in **#ap-desk**. To try it before using a real one, post
`examples/invoices/NOV-INV-1001.pdf` with the example config loaded.

## 4. Daily use

| You want to | Do this |
|---|---|
| Process an invoice | Post the PDF, scan or photo in #ap-desk. One thread per invoice |
| Approve or reject | Reply in the invoice's thread: `approve <reason>` / `reject <reason>` |
| See what's waiting | `uv run khata pending`, or the dashboard |
| Pay approved invoices | Download the NEFT CSV Khata posts in the thread; check it; upload it to your bank yourself |
| Add or update vendors | Edit `vendors.csv`, re-run `khata import vendors` |
| A vendor's bank account really changed | Call them on the number on file. If they confirm, the owner re-imports with `--allow-bank-change`; it's recorded in the audit log |
| Change approval limits | `uv run khata policy set auto_approve_below=5000 --as <owner> --reason "..."` (a new version; the old one is kept) |
| Let a vendor ask about their invoices | Add them to `contacts.csv`, `khata import contacts`, re-run `khata setup-switch`. See [vendor-inbox.md](vendor-inbox.md) |
| See the full history of an invoice | Dashboard → the invoice, or `uv run khata show <id>` |

**Scans and photos** are read with OCR (Tesseract, installed in the Docker image;
`brew install tesseract` or `apt install tesseract-ocr` otherwise). An invoice read by OCR
always goes to a person, because OCR can misread a digit. If a scan can't be read, Khata
says so in the thread and asks for the digital PDF.

**The Policy Steward** watches your decisions: when people approve the same price
exception for a vendor twice, it proposes a rule in #ap-desk, and the owner decides. See
[policy-steward.md](policy-steward.md).

## 5. Keep it safe

- **Back up daily.** `make backup` (or `./deploy/backup.sh` from cron) dumps Khata's and
  Switch's databases and the payment files into `backups/`, checks the dumps, and keeps
  14 days. Copy `backups/` to another machine or cloud storage. Restore with
  `./deploy/restore.sh backups/khata-<stamp>.dump`.
- **Upgrade** with `git pull`, then `make migrate` (the server container migrates by
  itself on start). Migrations never drop your data.
- **Secrets** live in `.env` and `switch/stack/.env`; never commit them. Keep `config/`
  private (bank details).
- **On a server**, only Caddy should be reachable (ports 80/443); the dashboard requires
  `KHATA_DASHBOARD_TOKEN`. In Mattermost's System Console, turn off open sign-up so only
  the accounts Khata created (and ones you invite) can join.
- **Who can do what** is enforced by Khata, not by the chat: only the person the policy
  names can approve a step, only the owner can change the policy or accept a bank change,
  and every action is in the audit log.

## Known limits

- One business per installation (run a second install for a second company).
- The NEFT CSV follows a common bulk-upload layout; check it against your bank's
  template before the first upload.
- No email inbox yet: invoices that arrive by email are forwarded or posted into the room.
- No accounting-software export yet (Tally, Zoho Books); the journal entries are in the
  payment batch and the database.
- The free LLM tiers limit daily volume (see above).

## If something goes wrong

| Symptom | Fix |
|---|---|
| `database schema is behind ... run khata db migrate` | `make migrate` |
| An invoice thread says it couldn't finish | Reply `retry` in the thread once the cause (often an LLM limit) has passed |
| Agents don't answer in the room | Is `make serve` running? `uv run khata -v serve` shows each connection |
| "not in the finance team" when approving | The username isn't in `khata.yaml`; add them and re-run `khata init` and `khata setup-switch` |
| An invoice from a new vendor is held (VENDOR_UNKNOWN) and can't be approved | Reject it, add the vendor to `vendors.csv` with bank details from your own records (not the invoice), `khata import vendors`, then post the invoice again |
| Every invoice is held as LOW_CONFIDENCE | One of the two readers is failing (often a daily limit): check `uv run khata show <id>` |
