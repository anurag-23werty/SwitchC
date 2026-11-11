"""Khata in Switch: nine agents working #ap-desk, #ap-audit and #ap-inbox. `uv run khata serve`.

Each Khata agent holds its own Agent Protocol connection to #ap-desk and speaks
in the room as itself. The Dispatcher listens to everything posted there
(`filter=all`); the others listen for messages addressed to them.

    a person posts a PDF            -> Dispatcher downloads it, starts the invoice's run
    each workflow step              -> that agent posts in the invoice's thread,
                                       handing off to the next agent by name
    the approval gate               -> Router tags the approver the policy names
    "approve <reason>" in the thread-> Dispatcher resumes the run; Khata checks the
                                       sender may make that decision
    a repeated approved exception   -> Steward posts a rule proposal; the owner's
                                       reply in its thread makes a new policy version
    a vendor writes in #ap-inbox    -> Liaison answers (code, no LLM), forwards PDFs
                                       to #ap-desk, alerts the owner on bank changes
    every gate, decision and draft  -> Router posts it to #ap-audit and refreshes
                                       the room documents (Postgres -> room)

Postgres is the system of record; Switch is the workplace. Without Switch there
is no way in for invoices and no way for a person to decide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from khata import liaison, repo, steward
from khata.config import ROOT, Settings
from khata.money import inr
from khata.runtime import Khata, Outcome
from khata.switch_client import KeyStore, RoomConnection, SwitchAgent, SwitchError
from khata.switch_docs import DOCS
from khata.switch_setup import AGENTS, AUDIT, DESK, SwitchState
from khata.switch_setup import INBOX as INBOX_ROOM

log = logging.getLogger("khata.room")

INBOX = ROOT / "data" / "inbox"
DISPATCHER, INTAKE, ROUTER, PAYER, STEWARD, LIAISON = (
    "khata-dispatcher",
    "khata-intake",
    "khata-router",
    "khata-payer",
    "khata-steward",
    "khata-liaison",
)
IGNORED_SENDERS = {"switch-admin", "system", "admin"}
FEED_AGENTS = {ROUTER, PAYER}  # the Steward feeds its own proposal posts
PROPOSAL_POST = re.compile(r"^\*\*Rule proposal #(\d+)\*\*")
HELP = (
    "I'm Khata's front desk. Post a vendor invoice as a PDF here and the team takes it "
    "from there, in its own thread. To decide an invoice the Router tags you on, reply "
    "in that thread with `approve <reason>` or `reject <reason>`."
)


@dataclass
class Job:
    kind: str  # invoice | decision | retry | rule
    thread: str
    sender: str
    message_id: str
    path: Path | None = None
    invoice_id: int | None = None
    decision: str | None = None
    reason: str | None = None
    proposal_id: int | None = None


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[-80:] or "invoice.pdf"


class KhataRoom:
    def __init__(self, settings: Settings, khata: Khata, state: SwitchState) -> None:
        self.s = settings
        self.k = khata
        self.desk = state.rooms[DESK]
        self.audit = state.rooms[AUDIT]
        keys = KeyStore(settings.agent_keys_path).load()
        missing = set(AGENTS) - set(keys)
        if missing:
            raise RuntimeError(
                f"agents not registered: {missing}; run `khata setup-switch`"
            )
        self.agents = {n: SwitchAgent(keys[n], settings.switch_api_url) for n in AGENTS}
        self.conns = {
            n: RoomConnection(a, self.desk, "all" if n == DISPATCHER else "addressed")
            for n, a in self.agents.items()
        }
        self.feed = RoomConnection(self.agents[ROUTER], self.audit, "addressed")
        # The vendor inbox: a Mattermost channel on the demo stack, or the room
        # Switch adopted for a Telegram group (KHATA_INBOX_ROOM_ID).
        self.inbox = os.environ.get("KHATA_INBOX_ROOM_ID") or state.rooms.get(
            INBOX_ROOM
        )
        self.inbox_conn = (
            RoomConnection(self.agents[LIAISON], self.inbox, "all")
            if self.inbox
            else None
        )
        self.ready = {
            n: asyncio.Event()
            for n in [*self.conns, "feed", *(["inbox"] if self.inbox else [])]
        }
        self.invoices: asyncio.Queue[Job] = asyncio.Queue()
        self.decisions: asyncio.Queue[Job] = asyncio.Queue()
        self.doc_ids: dict[str, str] = {}
        self._labels: dict[int, str] = {}

    # -- connections -----------------------------------------------------------

    async def _listen(self, name: str, conn: RoomConnection) -> None:
        async for ev in conn.events():
            if ev.kind == "connection_state":
                self.ready[name].set()
            elif name in (DISPATCHER, "inbox") and ev.kind == "message":
                try:
                    if name == DISPATCHER:
                        await self.on_message(ev.payload)
                    else:
                        await self.on_inbox(ev.payload)
                except Exception:  # one bad event must not stop the room
                    log.exception(
                        "could not handle message %s", ev.payload.get("message_id")
                    )
            if name == DISPATCHER and ev.sequence is not None:
                await self._save_cursor(DISPATCHER, self.desk, ev.sequence)
            elif name == "inbox" and ev.sequence is not None and self.inbox:
                await self._save_cursor(LIAISON, self.inbox, ev.sequence)

    async def _load_cursor(self, agent: str, room: str) -> int | None:
        async with self.k.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT cursor FROM switch_cursors WHERE agent = %s AND room_id = %s",
                    (agent, room),
                )
            ).fetchone()
        return row["cursor"] if row else None

    async def _save_cursor(self, agent: str, room: str, seq: int) -> None:
        async with self.k.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO switch_cursors (agent, room_id, cursor) VALUES (%s, %s, %s)
                   ON CONFLICT (agent, room_id)
                   DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()""",
                (agent, room, seq),
            )
            await conn.commit()

    async def run(self) -> None:
        # The Dispatcher picks up where it stopped: posts made while Khata was down
        # are delivered on reconnect (and switch_inbox makes repeats harmless).
        cursor = await self._load_cursor(DISPATCHER, self.desk)
        if cursor is not None:
            self.conns[DISPATCHER] = RoomConnection(
                self.agents[DISPATCHER], self.desk, "all", cursor=cursor
            )
            log.info("dispatcher resuming after event %s", cursor)
        listeners = [
            asyncio.create_task(self._listen(n, c), name=f"listen-{n}")
            for n, c in self.conns.items()
        ]
        listeners.append(
            asyncio.create_task(self._listen("feed", self.feed), name="feed")
        )
        if self.inbox:
            inbox_cursor = await self._load_cursor(LIAISON, self.inbox)
            if inbox_cursor is not None:
                self.inbox_conn = RoomConnection(
                    self.agents[LIAISON], self.inbox, "all", cursor=inbox_cursor
                )
            listeners.append(
                asyncio.create_task(
                    self._listen("inbox", self.inbox_conn), name="inbox"
                )
            )
        await asyncio.wait_for(
            asyncio.gather(*(e.wait() for e in self.ready.values())), timeout=60
        )
        log.info(
            "all %d agents connected to #%s and #%s%s",
            len(self.conns),
            DESK,
            AUDIT,
            f", the Vendor Liaison to the vendor inbox ({self.inbox})"
            if self.inbox
            else "",
        )
        await self.sync_docs()
        for invoice_id in await self.k.unfinished():
            thread = await self._thread_of(invoice_id)
            if thread:
                await self.invoices.put(
                    Job("retry", thread, "system", "", invoice_id=invoice_id)
                )
        workers = [
            asyncio.create_task(self._work(self.invoices), name="invoices"),
            asyncio.create_task(self._work(self.decisions), name="decisions"),
        ]
        await asyncio.gather(*listeners, *workers)

    async def aclose(self) -> None:
        for a in self.agents.values():
            await a.aclose()

    # -- room -> Khata -----------------------------------------------------------

    async def _claim(
        self, p: dict[str, Any], kind: str, room: str | None = None
    ) -> bool:
        """Record the post; False if it was handled before (a redelivery)."""
        async with self.k.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """INSERT INTO switch_inbox (message_id, room_id, thread_id, sender, kind, body)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (message_id) DO NOTHING RETURNING message_id""",
                    (
                        p["message_id"],
                        room or self.desk,
                        p.get("thread_id"),
                        p.get("sender_name") or "?",
                        kind,
                        (p.get("body") or "")[:4000],
                    ),
                )
            ).fetchone()
            await conn.commit()
        return row is not None

    async def _set_kind(
        self, message_id: str, kind: str, invoice_id: int | None
    ) -> None:
        async with self.k.pool.connection() as conn:
            await conn.execute(
                "UPDATE switch_inbox SET kind = %s, invoice_id = %s WHERE message_id = %s",
                (kind, invoice_id, message_id),
            )
            await conn.commit()

    async def _invoice_for_thread(self, thread: str) -> dict[str, Any] | None:
        async with self.k.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT id, state FROM invoices WHERE switch_thread_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (thread,),
                )
            ).fetchone()

    async def _proposal_for_thread(self, thread: str) -> dict[str, Any] | None:
        async with self.k.pool.connection() as conn:
            return await steward.for_thread(conn, thread)

    async def _thread_of(self, invoice_id: int) -> str | None:
        async with self.k.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT switch_thread_id FROM invoices WHERE id = %s", (invoice_id,)
                )
            ).fetchone()
        return row["switch_thread_id"] if row else None

    async def on_message(self, p: dict[str, Any]) -> None:
        sender = p.get("sender_name") or ""
        if sender == STEWARD and not p.get("thread_id") and p.get("message_id"):
            m = PROPOSAL_POST.match((p.get("body") or "").strip())
            if m:  # the proposal's own post: its id is the thread replies will carry
                async with self.k.pool.connection() as conn:
                    await steward.set_thread(
                        conn, int(m.group(1)), self.desk, p["message_id"]
                    )
                    await conn.commit()
        forwarded = sender == LIAISON and bool(p.get("attachments"))
        if (sender in AGENTS and not forwarded) or sender in IGNORED_SENDERS:
            return
        if not p.get("message_id"):
            return
        if not await self._claim(p, "received"):
            return
        msg_id = p["message_id"]
        thread = p.get("thread_id") or msg_id
        body = (p.get("body") or "").strip()
        desk = self.conns[DISPATCHER]
        attachments = p.get("attachments") or []

        if attachments:
            for att in attachments:
                res = await self.k.gw.call(
                    "dispatcher", "classify_input", file_name=att.get("filename") or ""
                )
                if res["kind"] != "invoice":
                    await desk.post(
                        f"@{sender} I only take invoices as PDF; `{att.get('filename')}` "
                        f"is {res['reason']}.",
                        thread,
                    )
                    continue
                data = await self.agents[DISPATCHER].download_media(
                    self.desk, att["mxc"]
                )
                INBOX.mkdir(parents=True, exist_ok=True)
                path = INBOX / (
                    f"{hashlib.sha256(data).hexdigest()[:12]}_{_safe_name(att['filename'])}"
                )
                path.write_bytes(data)
                await desk.address(
                    (
                        f"New invoice from the vendor inbox (via @{sender}): "
                        if forwarded
                        else f"New invoice from @{sender}: "
                    )
                    + f"`{att['filename']}`. Over to you.",
                    [INTAKE],
                    thread,
                )
                await self.invoices.put(
                    Job("invoice", thread, sender, msg_id, path=path)
                )
            await self._set_kind(msg_id, "invoice", None)
            return

        proposal = (
            await self._proposal_for_thread(thread) if p.get("thread_id") else None
        )
        if proposal is not None:
            res = await self.k.gw.call(
                "dispatcher", "classify_input", text=body, in_invoice_thread=True
            )
            if res["kind"] == "decision":
                await self._set_kind(msg_id, "rule_decision", None)
                await self.decisions.put(
                    Job(
                        "rule",
                        thread,
                        sender,
                        msg_id,
                        decision=res["decision"],
                        reason=res["reason"],
                        proposal_id=proposal["id"],
                    )
                )
            else:
                await self._set_kind(msg_id, "chat", None)
            return

        inv = await self._invoice_for_thread(thread) if p.get("thread_id") else None
        if inv is None:
            addressed = p.get("addressed") or f"@{DISPATCHER}" in body
            await self._set_kind(msg_id, "chat", None)
            if addressed:
                await desk.post(HELP, thread)
            return

        res = await self.k.gw.call(
            "dispatcher", "classify_input", text=body, in_invoice_thread=True
        )
        if res["kind"] == "decision":
            await self._set_kind(msg_id, "decision", inv["id"])
            await self.decisions.put(
                Job(
                    "decision",
                    thread,
                    sender,
                    msg_id,
                    invoice_id=inv["id"],
                    decision=res["decision"],
                    reason=res["reason"],
                )
            )
        elif (
            re.fullmatch(r"(?i)(@\S+\s+)?retry\W*", body) and inv["state"] == "RECEIVED"
        ):
            await self._set_kind(msg_id, "retry", inv["id"])
            await self.invoices.put(
                Job("retry", thread, sender, msg_id, invoice_id=inv["id"])
            )
        elif inv["state"] in ("AWAITING_APPROVAL", "ON_HOLD"):
            await self._set_kind(msg_id, "hint", inv["id"])
            await desk.post(
                f"@{sender} to decide, start your reply with `approve` or `reject`, then "
                "the reason. Anything else I leave for the people in this thread.",
                thread,
            )
        else:
            await self._set_kind(msg_id, "chat", inv["id"])

    # -- the vendor inbox (Vendor Liaison) ------------------------------------------

    async def on_inbox(self, p: dict[str, Any]) -> None:
        """A vendor's message. Plain code; nothing here reaches an LLM."""
        sender = p.get("sender_name") or ""
        if sender in AGENTS or sender in IGNORED_SENDERS or not p.get("message_id"):
            return
        if not await self._claim(p, "vendor", self.inbox):
            return
        msg_id = p["message_id"]
        thread = p.get("thread_id") or msg_id
        body = (p.get("body") or "").strip()
        out = self.inbox_conn
        assert out is not None
        async with self.k.pool.connection() as conn:
            staff = await repo.person(conn, sender)
            contact = await repo.vendor_contact(conn, sender)
            company = (await repo.get_company(conn)).name
            owner = (await repo.get_policy(conn)).approvers["owner"]
        if staff is not None:
            await self._set_kind(msg_id, "staff", None)
            return
        if contact is None:
            await self._set_kind(msg_id, "unregistered", None)
            await out.post(liaison.not_registered(company), thread)
            return
        pdfs = [
            a
            for a in p.get("attachments") or []
            if (a.get("filename") or "").lower().endswith(".pdf")
        ]
        intent = liaison.classify(body, bool(pdfs))
        await self._set_kind(msg_id, f"vendor_{intent.kind}", None)
        if intent.kind == "invoice_upload":
            for att in pdfs:
                data = await self.agents[LIAISON].download_media(self.inbox, att["mxc"])
                await self.agents[LIAISON].upload_media(
                    self.desk,
                    [(att["filename"], data, "application/pdf")],
                    liaison.upload_caption(contact, att["filename"]),
                    None,
                )
                await out.post(liaison.upload_ack(att["filename"]), thread)
        elif intent.kind == "bank_change":
            await out.post(liaison.bank_change_reply(contact), thread)
            await self.conns[LIAISON].post(
                liaison.bank_change_alert(contact, owner, body), None
            )
            await self.feed.post(
                f"**Vendor Liaison**: {contact['vendor_name']} asked in the vendor inbox to "
                "change bank details. Refused; owner alerted to call the number on file.",
                None,
            )
            async with self.k.pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO audit_log (actor, actor_kind, action, reason, details)
                       VALUES (%s, 'agent', 'vendor_bank_change_request', %s, %s)""",
                    (
                        LIAISON,
                        f"{sender} ({contact['vendor_code']}) asked to change bank details",
                        json.dumps({"message_id": msg_id, "text": body[:1000]}),
                    ),
                )
                await conn.commit()
        elif intent.kind == "status":
            lines = []
            for no in intent.invoice_nos or [None]:
                res = await self.k.gw.call(
                    "vendor_liaison",
                    "get_invoice_status",
                    vendor_code=contact["vendor_code"],
                    invoice_no=no,
                )
                if res["matches"]:
                    lines += [
                        liaison.status_line(r) for r in res["matches"][: 1 if no else 5]
                    ]
                elif no:
                    lines.append(
                        f"{no}: I can't find this one from {contact['vendor_name']}. If it "
                        "hasn't reached us, send it here as a PDF."
                    )
            await out.post(
                "\n".join(lines) or "I don't have any invoices from you yet.", thread
            )
        else:
            await out.post(liaison.help_text(contact), thread)

    # -- Khata -> room ---------------------------------------------------------------

    async def _label(self, invoice_id: int) -> str:
        if invoice_id not in self._labels:
            inv = await self.k.gw.call("analyst", "get_invoice", invoice_id=invoice_id)
            if not inv.get("invoice_no"):
                return f"invoice #{invoice_id}"
            self._labels[invoice_id] = (
                f"{inv['vendor_code'] or inv['vendor_name'] or '?'} {inv['invoice_no']} "
                f"₹{inr(Decimal(str(inv['total'])))}"
            )
        return self._labels[invoice_id]

    def _speaker(self, agent: str) -> RoomConnection:
        return self.conns.get(agent) or self.conns[DISPATCHER]

    async def speak(
        self, thread: str, invoice_id: int | None, line: dict[str, str]
    ) -> None:
        conn = self._speaker(line["agent"])
        if line.get("to") in self.conns:
            await conn.address(line["text"], [line["to"]], thread)
        else:
            await conn.post(line["text"], thread)
        if line["agent"] in FEED_AGENTS and invoice_id is not None:
            who = "" if line["agent"] == ROUTER else "Payer: "
            await self.feed.post(
                f"**{await self._label(invoice_id)}**: {who}{line['text']}", None
            )

    async def _after(self, job: Job, out: Outcome) -> None:
        router = self.conns[ROUTER]
        if out.paused and out.request and not out.request.get("error"):
            await router.post(out.request["message"], job.thread)
            await self.feed.post(
                f"**{await self._label(out.invoice_id)}**: waiting for "
                f"@{out.request['next_approver']} ({out.request['next_step']} step).",
                None,
            )
        payment = out.values.get("payment") or {}
        if payment.get("drafted") and payment.get("csv_path"):
            csv = Path(payment["csv_path"])
            if csv.exists():
                await self.agents[PAYER].upload_media(
                    self.desk,
                    [(csv.name, csv.read_bytes(), "text/csv")],
                    f"NEFT bulk-upload draft {payment['batch_ref']}. Review it, then upload it "
                    "to the bank yourself. Khata never pays.",
                    job.thread,
                )
        proposal = out.values.get("proposal")
        if proposal:
            await self._post_proposal(proposal["id"])
        await self.sync_docs(only=("khata-decisions-log", "khata-vendor-knowledge"))

    async def _post_proposal(self, proposal_id: int) -> None:
        """The Steward's proposal, as its own thread in #ap-desk, tagging the owner."""
        async with self.k.pool.connection() as conn:
            p = await steward.describe(conn, proposal_id)
        if p["switch_thread_id"] or p["status"] != "proposed":
            return
        res = await self.conns[STEWARD].post(p["message"], None)
        if res.get(
            "event_id"
        ):  # replaced by the message id when the post's event arrives
            async with self.k.pool.connection() as conn:
                row = await steward.for_thread(conn, res["event_id"])
                if row is None:
                    await conn.execute(
                        "UPDATE rule_proposals SET switch_room_id = %s, switch_thread_id = "
                        "COALESCE(switch_thread_id, %s) WHERE id = %s",
                        (self.desk, res["event_id"], proposal_id),
                    )
                    await conn.commit()
        await self.feed.post(
            f"**Policy Steward**: rule proposal #{proposal_id} for @{p['owner']}: "
            f"{p['summary']}.",
            None,
        )

    async def _work(self, q: asyncio.Queue[Job]) -> None:
        while True:
            job = await q.get()
            try:
                await self._do(job)
            except Exception as e:
                log.exception("job %s failed", job)
                await self.conns[INTAKE].post(
                    f"I couldn't finish this one ({type(e).__name__}: {str(e)[:300]}). "
                    "Nothing was decided or paid. Reply `retry` in this thread to try again.",
                    job.thread,
                )
            finally:
                q.task_done()

    async def _do(self, job: Job) -> None:
        async def on_log(invoice_id: int | None, line: dict[str, str]) -> None:
            await self.speak(job.thread, invoice_id, line)

        if job.kind == "invoice":
            out = await self.k.submit(
                job.path, room_id=self.desk, thread_id=job.thread, on_log=on_log
            )
            await self._set_kind(job.message_id, "invoice", out.invoice_id)
        elif job.kind == "rule":
            await self._decide_rule(job)
            return
        elif job.kind == "retry":
            out = await self.k.retry(job.invoice_id, on_log=on_log)
        else:
            current = await self.k.outcome(await self.k.thread_for(job.invoice_id))
            if not current.paused:
                await self.conns[ROUTER].post(
                    f"@{job.sender} this invoice isn't waiting for a decision "
                    f"(it is {current.state}).",
                    job.thread,
                )
                return
            out = await self.k.decide(
                job.invoice_id,
                job.sender,
                job.decision,
                job.reason,
                message_id=job.message_id,
                on_log=on_log,
            )
        await self._after(job, out)

    async def _decide_rule(self, job: Job) -> None:
        assert job.proposal_id is not None
        st = self.conns[STEWARD]
        try:
            res = await self.k.decide_rule(
                job.proposal_id,
                job.sender,
                job.decision or "",
                job.reason or "",
                message_id=job.message_id,
            )
        except steward.ProposalRefused as e:
            await st.post(f"@{job.sender} not recorded: {e}.", job.thread)
            return
        text = steward.decision_message(res)
        await st.post(text, job.thread)
        await self.feed.post(
            f"**Policy Steward**: {job.sender} {res['status']} rule proposal "
            f"#{job.proposal_id} ({job.reason}). "
            + (
                f"Policy v{res['new_version']} is active."
                if res["new_version"]
                else "Policy unchanged."
            ),
            None,
        )
        await self.sync_docs(only=("khata-approval-policy", "khata-vendor-knowledge"))

    # -- room documents (Postgres -> room) -----------------------------------------

    async def sync_docs(self, only: tuple[str, ...] | None = None) -> None:
        router = self.conns[ROUTER]
        if not self.doc_ids:
            refs = await router.op("list_references")
            for d in refs.get("documents") or []:
                if d.get("name") in DOCS:
                    self.doc_ids[d["name"]] = d["id"]
        for name, (description, instructions, build) in DOCS.items():
            if only and name not in only:
                continue
            async with self.k.pool.connection() as conn:
                content = await build(conn)
            try:
                if name in self.doc_ids:
                    await router.op(
                        "update_room_document",
                        document_id=self.doc_ids[name],
                        content=content,
                    )
                else:
                    res = await router.op(
                        "create_room_document",
                        name=name,
                        description=description,
                        instructions=instructions,
                        content=content,
                    )
                    self.doc_ids[name] = res["document_id"]
            except SwitchError:
                log.exception("room document %s not synced", name)


async def serve(settings: Settings) -> None:
    state = SwitchState.load()
    async with Khata.open(settings) as k:
        room = KhataRoom(settings, k, state)
        try:
            await room.run()
        finally:
            await room.aclose()
