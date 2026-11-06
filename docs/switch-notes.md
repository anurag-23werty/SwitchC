# Switch notes — what Khata relies on, verified

Verified on 2026-11-06 against **switch-core 0.31.0** (the newest server version
published to `ghcr.io/sandbox-quantum`), from three sources:

1. The official docs, `docs/official/` in the repo (the same content served at
   docs.switchagents.ai and its MCP server).
2. The server code: `core/switch_core/bridges/agent/api/handlers.py` (HTTP
   surface), `bridges/agent/operations/definitions.py` (operations),
   `addressing.py` (who may address an agent), `docs/TEMPLATES.md` (templates).
3. **Live tests** against a local stack (`make up` + `make hello`). Every item
   marked ✅ was observed working, not just read.

---

## Q1. Agent Protocol: endpoints, auth, messages, events, addressing

**Transport.** One SSE stream down, HTTP calls up. Every call carries the agent's
API key as `Authorization: Bearer <key>`. The registration token is used once,
to register, and is refused everywhere else. `GET /health` is public and is the
probe for the bridge base URL (`http://localhost:8000` locally).

**Registering a custom agent** ✅
```
POST /agents                      Bearer <AGENT_REGISTRATION_TOKEN>
{ "name": "khata-intake", "description": "...", "display_name": "Khata Intake",
  "icon_url": null, "connector_type": "khata",
  "integration_profile": { "connection_model": "always_on", "message_exchange": true,
    "pre_invocation_mediation": [], "post_invocation_mediation": [], "event_reporting": [] } }
→ 200 { "id": "<uuid>", "api_key": "<shown once>" }
```
- `POST /agents/register-known` is **not** for us: it only accepts the CLI
  providers (`claude-code`, `codex`, `opencode`, ...) that Switch Console runs.
- `connector_type` is a free string. `always_on` is the right connection model
  for a long-running service (it's what Switch's own load-test agents use).
- Names match `^[a-z0-9][a-z0-9._-]*$`. Re-registering a name is 409 unless
  `overwrite: true`, which issues a new key. Khata caches keys in
  `.khata-agents.json` (gitignored) because the key is returned once.

**Opening the event stream** ✅
```
GET /agents/{id}/events?connection_id=<uuid>&scope=single&filter=addressed
    &rooms=<room_id>&start_from=head&client=khata&client_version=0.1.0
Accept: text/event-stream     [Last-Event-ID: <seq> on reconnect]
```
- The client generates the connection id and **reuses it on reconnect**.
  Reopening a live id is a takeover (the old stream gets `evicted`).
- **Declare rooms on the URL**, not after: catch-up runs at open.
- First frame is always `connection_state` (carries `heartbeat_interval_seconds`,
  `rooms`, `cursor`). Read it rather than assuming.
- Max 32 connections per agent.

**Heartbeat** ✅ `POST /agents/{id}/connection/beat {"connection_id","cursor"}`
every 2 s (TTL 6 s). 404/409 mean *reopen the stream*, never back off.
Keepalives are SSE comment lines every 15 s, not events.

**Events** (domain events carry an SSE `id:` = `sequence`):
- `message` payload ✅: `addressed`, `sender`, `sender_name`, `message_id`,
  `body`, `timestamp`, `thread_id`, `attachments[]`. Observed:
  `sender_name: "user"` (the Mattermost username).
- `command`, `room_join`, and `task_*` (task protocol "present but not ready — build
  nothing on it").
- Control frames: `connection_state`, `gap`, `evicted`, `subscription_changed`,
  `room_released`.

**Calling operations** ✅
```
POST /agents/{id}/ops/{operation}
X-Switch-Connection-Id: <connection id>      ← binds the call to that room
<arguments object, unwrapped>
→ 200 { "result": ... }      (404 unknown op, 400 bad args, 403 denied, 409 dead connection)
```
`GET /agents/{id}/ops` lists the whole registry with JSON schemas.

**Posting a reply** ✅
- `post_message(body, thread_id?)` — threaded reply into the connection's room.
  Any message id in a thread works; it is normalised to the root.
- `send_targeted_message(body, target_names?, target_roles?, thread_id?)` —
  prepends `@name` for each target so they receive it as **addressed**. This is
  how one Khata agent hands off to another.
- `POST /agents/{id}/message {room_id, content}` — no connection needed, no thread.

**Addressing someone by name.** `@name`, `@alias` or `@role`, case-insensitive;
trailing `,` and `:` are fine, **a trailing `.` breaks it** (dots are legal in
names). Backticks don't neutralise a mention. Captions on file uploads count.

**Who may address an agent (important).** ✅ observed
A newly registered agent is **owner-only**. Our first test was refused in-thread:
*"You're not permitted to direct messages to me in this room — my operator has
restricted who can address me here."* Policies are set by the owner/admin via the
Gateway, not by the agent:
```
POST /gateway/auth/login {email, password}               (session cookie)
PUT  /gateway/agents/{agent_id}/addressing-policy
     {"policy": {"rules": [{"rooms": [<room ids>], "room_groups": "*",
                            "users": "*", "agents": "*"}]}}
```
`users: "*"` also admits Mattermost accounts with no linked Switch user (an
unlinked account otherwise "matches nobody"). Scoping by `rooms` keeps each
Khata agent deaf everywhere else. Implemented in `khata/switch_admin.py`.

## Q2. Can custom agents read/write room Rules, Tasks, Decisions, Knowledge?

**Switch has no objects called Rules, Decisions or Knowledge.** What a room holds:

| Switch concept | Agent can read | Agent can write | Notes |
|---|---|---|---|
| Room **instructions** | ✅ via `connect_to_room` | `update_room` (room write access) | The briefing every agent reads on join |
| **Room-scoped documents** | `load_internal_documents` | `create_room_document`, `update_room_document`, `delete_room_document` | Name unique per room, **≤ 1 MB**, only the **creating agent** can edit it. Humans see them with `!list-documents` |
| **Library documents / references** | yes | create yes; packages are Gateway-only | Reference types are a closed set: Google Drive, Confluence, GitHub, Jira |
| **Roles** (jobs agents hold) | `list_roles` | `define_role`, `assume_role` | For agents, not humans |
| **Aliases** | yes | yes | Short room-scoped handles |
| **Tasks** | — | — | "Present but not ready for use." **We will not use them.** |

**Khata's mapping (decision):**
- *Rules* → the approval policy lives versioned in Khata's `rules` table (source
  of truth) and is mirrored into a room-scoped document `khata-approval-policy`
  plus a summary in the room instructions.
- *Decisions* → `decisions` + `audit_log` tables in Khata's Postgres; mirrored as
  a running room-scoped document `khata-decisions-log` and posted in `#ap-audit`.
- *Knowledge* → `vendor_knowledge` (pgvector) in Khata's Postgres; a readable
  digest kept in a room document `khata-vendor-knowledge`.
- *Tasks* → the invoice state machine (LangGraph + Postgres) and one **thread per
  invoice** in the room. Not Switch tasks.
- One agent (the Router / "recorder") creates and owns these documents, since
  only the creating agent can update a room-scoped document.

## Q3. How files posted in a room reach an agent ✅

Observed end to end: a PDF uploaded in Mattermost with the caption
`@khata-hello here is an invoice` arrived as **one** addressed `message` event:
```json
"attachments": [{"filename": "test-invoice.pdf", "mimetype": "application/pdf",
  "size": 60, "mxc": "switch-media://99f8…", "msgtype": "m.file"}]
```
An attachment is a **pointer, never bytes**. Fetch it with
`GET /agents/{id}/rooms/{room_id}/media?mxc=<mxc>` → raw bytes (got `%PDF-…`).
Agents can post files back with multipart `POST /agents/{id}/rooms/{room_id}/media`
(`files`, optional `caption`, `thread_id`) — Payer will use this for the NEFT CSV.
Several files in one post arrive as separate events sharing
`com.switch.attachment_group {id, index, total}`; coalesce by group.

## Q4. Valid template values (`provider`, `bridge`, `addressing`)

From `docs/TEMPLATES.md`:
- `provider`: `claude | codex | opencode | antigravity | cursor` — **CLI agents that
  Switch Console runs**. Custom protocol agents have no provider.
- `bridge`: the display name of a messaging app set up on the server; omit it for
  the server's default (our bundled Mattermost). Default `[$first]`.
- `addressing`: `owner` (default) | `owner-agents` | `anyone`, and in version 2
  `{anyone_in: [room keys]}`.
- `room:` fields: `name, description, instructions, bridge, channel_type,
  read/write_visibility, agents, users, roles, references, packages, docs, aliases`;
  plus `kickoff:` (posted for the deployer once members join; agents only wake if
  mentioned and their addressing admits it). `group:` + `rooms:` + `links:` for
  several rooms.

**Decision.** Because Khata's agents are custom (no `provider`), the template's
`agents:` block doesn't apply. `switch/khata.template.yaml` will define the
**rooms** (`#ap-desk`, `#ap-audit`) with `instructions`, `docs`, `aliases`, the
existing Khata agent names and the human users, plus the kickoff. Setup order:
register agents → create rooms from the YAML (`create_room_from_yaml` op, or
`POST /gateway/rooms/from-yaml`) → set addressing policies scoped to those room
ids → post kickoff. To verify in Phase 4: whether the kickoff can address our
agents before their policy exists (fallback: create without `kickoff:` and post
it ourselves after setting policy).

## Q5. Running Switch core remotely

- **One Linux VM, Docker Compose** — the published
  `ghcr.io/sandbox-quantum/standalone-compose` (same file we vendor in
  `switch/stack/`). Ports 8000 (API) and 3000 (Gateway) bind to `127.0.0.1`;
  set `SWITCH_BIND_ADDR` and put TLS + auth in front (Caddy, for us).
- **Kubernetes** — Helm chart `oci://ghcr.io/sandbox-quantum/charts/switch`.
- **Switch Console "remote host"** — onboard an SSH host (Ubuntu 22.04/24.04 or
  macOS, 2 vCPU / 4 GB / 20 GB suggested); no restart policy, a reboot stops it.
- Before first start: set `AGENT_REGISTRATION_TOKEN`, guard `JWT_SECRET_KEY` /
  `SECRET_KEYS`, and pick `ID_SERVER_NAME` (cannot change later).
- Back up Postgres: it holds rooms, messages *and* files.

**Decision:** local stack for development and the demo; remote VM is a Phase 6
stretch.

---

## How Khata uses Switch (the plan these findings lead to)

- Each Khata agent (`khata-intake`, `khata-verifier`, `khata-analyst`,
  `khata-auditor`, `khata-router`, `khata-payer`) is its own registered Switch
  participant with a display name and icon, so the room shows who did what.
- One `scope=single` connection per (agent, room). Worker agents use
  `filter=addressed`; the Router uses `filter=all` in `#ap-desk` so it can read
  a human's plain-text "approve"/"reject" reply in an approval thread it opened
  (delivery and acting are separate; Switch documents this).
- Hand-offs are `send_targeted_message` to the next agent, in the invoice's
  thread. The thread is the invoice's conversation of record.
- Files posted in `#ap-desk` start the workflow (Q3).
- Approvers are identified from `sender_name` (Mattermost username) mapped to a
  Khata role in our DB; Khata, not the LLM, checks that the sender may approve.

## Gotchas found while testing

- `create_room` returns `{"id": ...}`, but `list_rooms` entries use `"room_id"`.
- Registering through the env `AGENT_REGISTRATION_TOKEN` and then addressing the
  agent from the Gateway admin's linked Mattermost account was still refused as
  not-owner; an explicit room-scoped policy fixed it. (Not investigated further —
  the explicit policy is what we want anyway.)
- Mattermost 11.9 has only an amd64 image; on Apple Silicon it runs emulated and
  takes ~1 min to become ready. `setup` waits for it.
- The stack needs `SWITCH_VERSION` set — no default by design. We pin `0.31.0`.

## Open items for Phase 4

1. Does an `@username` in an agent's message notify a Mattermost user? (Router
   tags Priya/Owner this way.) Fallback: Mattermost API post as a mention-only
   message, or a DM room per approver via `create_room(channel_type="direct")`.
2. Creating extra Mattermost users (Ravi, Priya, Owner) for the demo — via the
   Mattermost API in our seed script, then `add_users_to_room`.
3. Kickoff vs. addressing order (see Q4).
