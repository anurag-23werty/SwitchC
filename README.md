# Khata — an agentic accounts-payable room on Switch

> Status: Phase 3 (LangGraph agent runtime with live LLMs, approval gate that survives restarts) done. The full README lands in Phase 7.

Khata turns a finance team's chat channel into a [Switch](https://github.com/sandbox-quantum/switch)
room where AI agents receive, read, verify and match vendor invoices, explain exceptions,
route approvals to the right person, and draft payment batches — while people make every
money decision.

Built for the WeMakeDevs **Flip the Switch** hackathon (Track 1: Best Business Workflow).

## Run it (so far)

Requires Docker, [uv](https://docs.astral.sh/uv/) and `make`.

```sh
make env      # generates Switch's server secrets and Khata's .env
make up       # Switch core + Mattermost + Gateway, and Khata's Postgres
uv sync
make seed     # Khata's database + 31 invoice PDFs + evals/golden.yaml
make hello    # a custom agent joins Mattermost through the Switch Agent Protocol
make test
```

The demo business, vendors, approval policy and all 31 labelled invoices are described
in [docs/demo-data.md](docs/demo-data.md). The checks, approval policy, permissions and MCP
tools are in [docs/tools-and-policy.md](docs/tools-and-policy.md).

```sh
make mcp AGENT=analyst   # Khata's tools over MCP, only the ones that agent may call
make demo                # D01-D09 through the agent workflow with live LLMs (no Switch yet)
uv run khata pending     # approvals waiting for a person
uv run khata decide 10 --as priya approve Matches PO and goods received
```

How the agents run (two-model extraction, Analyst and Auditor on different
providers, the approval gate, retries, Prompt Guard) is in
[docs/agent-runtime.md](docs/agent-runtime.md). LLM keys go in `.env`
(`GROQ_API_KEY`, `OPENROUTER_API_KEY`; both free, no card).

Open Mattermost at http://localhost:8065 and sign in as `user` (password:
`MATTERMOST_USER_PASSWORD` in `switch/stack/.env`). In the `khata-hello` channel, send
`@khata-hello hi` — the agent replies in the thread.

See [docs/switch-notes.md](docs/switch-notes.md) for exactly how Khata uses the protocol.
