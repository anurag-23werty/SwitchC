# Khata in Switch (Phase 4)

Khata's nine agents are Switch participants. Invoices only get in through a
Switch room, and people only decide in one. Postgres is the system of record;
Switch is where the work happens.

```sh
make up && make seed
make setup-switch     # agents, the team's accounts, #ap-desk + #ap-audit from the template
make serve            # the agents go to work (Ctrl-C to stop; restart any time)
```

Open http://localhost:8065, sign in as `ravi` (password: `KHATA_DEMO_PASSWORD` in
`.env`), and drop an invoice PDF into **#ap-desk**. Sign in as `priya` or `owner`
to answer approvals. To run all nine demo invoices without clicking, with the
labelled replies playing the approvers:

```sh
uv run python scripts/room_demo.py
```

## Who is in the room

| Agent | Listens to | Does |
|---|---|---|
| `khata-dispatcher` | everything in #ap-desk (`filter=all`) | Front door. Downloads PDFs, starts runs, turns `approve`/`reject` replies into decisions, ignores chat |
| `khata-intake` | messages addressed to it | Two-model reading, GST and injection checks |
| `khata-verifier` | addressed | Three-way match, duplicate and bank checks |
| `khata-router` | addressed (#ap-desk and #ap-audit) | Policy outcome, tags the approver, records decisions, writes the feed and the room documents |
| `khata-analyst` | addressed | The note for the approver, citing past decisions |
| `khata-auditor` | addressed | Second opinion on the note, on another LLM provider |
| `khata-payer` | addressed | Posts the NEFT draft CSV into the thread. Never pays |
| `khata-steward` | addressed (#ap-desk and #ap-audit) | Proposes a rule after repeated approvals of the same exception; only the owner can accept it (Phase 6, [policy-steward.md](policy-steward.md)) |
| `khata-liaison` | all (#ap-inbox), addressed (#ap-desk) | Answers vendors about their own invoices, forwards their PDFs, refuses bank changes and alerts the owner (Phase 6, [vendor-inbox.md](vendor-inbox.md)) |

Each agent is registered over the Agent Protocol (`POST /agents`), holds its own
connection to the room (an SSE stream plus a heartbeat), and posts as itself, with
its own Mattermost bot and avatar. The work runs in Khata's LangGraph workflow
(docs/agent-runtime.md). Each step's message is posted by the agent that did it,
and it hands off to the next agent **by name** with `send_targeted_message`:

```
<ravi>             New invoice  [D03_MST_MST-INV-7731.pdf]
<khata-dispatcher> @khata-intake New invoice from @ravi: `D03_MST_MST-INV-7731.pdf`. Over to you.
<khata-intake>     Read MST/INV/7731 from Mumbai Steel Traders, total ₹73,632.00. Both readers agree (...)
<khata-intake>     @khata-verifier GST and document checks: passed.
<khata-verifier>   @khata-router PO/GRN match, duplicate and bank checks: three_way_match: PRICE_VARIANCE.
<khata-router>     @khata-analyst Awaiting Approval for Finance manager. price is over the PO beyond tolerance.
<khata-analyst>    @khata-auditor Freight not on PO and price 4.00% above PO ...
<khata-auditor>    @khata-router Agrees with the Analyst's note.
<khata-router>     Mumbai Steel Traders MST/INV/7731 for ₹73,632.00 needs finance manager approval (@priya). ...
<priya>            approve Freight was agreed on a call with Mumbai Steel. OK to pay freight up to 4% on their orders.
<khata-router>     @khata-payer priya approved the manager step: Freight was agreed ...
<khata-payer>      Drafted NEFT batch KH-20261106-003 for ₹73632.00 ... Nothing has been paid.
<khata-payer>      NEFT bulk-upload draft KH-20261106-003. ...  [KH-20261106-003.csv]
```

## The rooms: `switch/khata.template.yaml`

A Switch template (version 1, a room **group** "Khata AP"), deployed by
`khata setup-switch` through the Gateway's `POST /gateway/rooms/from-yaml`:

- **#ap-desk**: all the agents, the deployer, Priya (finance manager), the owner and
  Ravi (clerk); instructions describing each agent and how to approve; aliases
  (`@desk`, `@router`); a kickoff that states the approval policy in force.
  The `policy` param is filled in from Khata's `rules` table at deploy time, so the
  kickoff never disagrees with the database.
- **#ap-audit**: the Router, Auditor and Steward plus the approvers: the decision feed.
- **#ap-inbox** (its own single-room template, `switch/khata-inbox.template.yaml`):
  the Liaison and the registered vendor contacts. Bridged to Telegram in production.
- A link from #ap-desk to #ap-audit.

The agents aren't created by the template (it has no `agents:` block). Switch's
template agents run on CLI providers (claude, codex, ...), and Khata's are custom
protocol agents, so the template names them as existing agents.

`setup-switch` also creates the team's Mattermost accounts, sets each agent's
addressing policy to "anyone in #ap-desk or #ap-audit" (new agents are owner-only),
and uploads the avatars through Mattermost's API. Switch fetches `icon_url` from the
server side behind an outbound-address policy, so a laptop URL wouldn't work. Every
step checks what exists first, so it's safe to re-run.

## Decisions: room → Postgres → room

- **Room → Postgres.** Every human post Khata acts on goes into `switch_inbox`, keyed
  by Switch's message id (so a redelivered event is handled once). A decision is
  stored in `decisions` with the `switch_message_id` it came from.
- **Who may decide.** The sender is the Mattermost username Switch reports. Khata, not
  an LLM, checks it against `people` and the pending step: the clerk can't approve,
  Priya can't approve the owner's step, and every decision needs a reason. A refused
  reply gets a "Not recorded: …" answer in the thread, and the run stays paused.
  A reply that isn't a decision ("looks fine") gets a hint, and nothing happens.
- **Postgres → room.** The Router writes three room documents in #ap-desk, regenerated
  from Postgres after every run step: `khata-approval-policy` (rules table, all
  versions), `khata-decisions-log`, and `khata-vendor-knowledge`. Switch has no
  Rules/Decisions/Knowledge objects, and room documents are what every member can
  read (see docs/switch-notes.md, Q2). It also posts each gate outcome, decision and
  payment draft to **#ap-audit**.

## Surviving a restart

- Paused approvals live in the LangGraph checkpoints (Postgres).
- The Dispatcher stores its event-stream cursor in `switch_cursors` after every event
  and reopens from it. Posts made while Khata was down arrive on restart. Tested live:
  Priya approved G12 while the service was stopped; on restart her approval was
  recorded and the owner was asked next.
- Runs that stopped mid-way (a crash, both LLM providers down) are re-run at startup.
  A failed job says so in its thread ("Nothing was decided or paid. Reply `retry`").

## What we learned about Switch here

- An `@username` in an agent's post does notify the Mattermost user (Priya's
  `mention_count` went up). This answers the Phase 0 open question.
- Switch message ids aren't Mattermost post ids. Khata keys everything on Switch's ids.
- `list_references` lists a room's documents, so a restarted Router finds and updates its
  documents instead of creating duplicates.
- A redeployed template creates new rooms; `setup-switch` looks up existing `ap-desk`
  and `ap-audit` first.

## Tests

`tests/test_switch_service.py` (8) feeds Switch-shaped events to the real Dispatcher with
fake connections and a real runtime/DB: a PDF runs the workflow with by-name hand-offs and
a CSV upload; a redelivered event is handled once; "looks fine" gets a hint; the clerk's
approval is refused in-thread; Priya's approval resumes the run and is stored with its
Switch message id; the audit feed and the room documents are written; non-PDFs are
declined and agent posts ignored; a failed job says so; the cursor persists.

The live acceptance run is `scripts/room_demo.py`: D01–D09 posted in Mattermost by Ravi,
the labelled approvers replying in the threads, outcomes read from Khata's database.
