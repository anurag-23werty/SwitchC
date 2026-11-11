"""Khata's tools. Each loads what it needs, runs a deterministic check, and records the result.

Tools never take amounts or flags from an agent's say-so: they read the invoice
from the database by id. An agent can ask for a check; it can't supply the answer.
Every tool here is reached through `khata.policy.ToolGateway`, which enforces
the per-agent allowlist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
from psycopg_pool import AsyncConnectionPool

from khata import repo, steward
from khata.approval import route_approval as apply_policy
from khata.checks.bank import check_bank_change as bank_check
from khata.checks.duplicate import check_duplicate as dup_check
from khata.checks.gst import is_valid_gstin
from khata.checks.gst import validate_gst as gst_check
from khata.checks.injection import detect_injection as injection_check
from khata.checks.matching import three_way_match as match_check
from khata.embeddings import Embedder, default_embedder, to_pgvector
from khata.models import CheckReport, Flag, InvoiceData, State
from khata.payments import NotPayable
from khata.payments import draft_payment as draft
from khata.policy import ToolFn
from khata.state import audit, transition

logger = logging.getLogger(__name__)

ROLE_FOR_STEP = {"manager": "finance_manager", "owner": "owner"}


class DecisionRefused(Exception):
    """A person tried to make a decision they aren't entitled to make."""


def _report(r: CheckReport) -> dict[str, Any]:
    return {
        "check": r.check,
        "flags": [f.value for f in r.flags],
        "findings": [f.model_dump(mode="json") for f in r.findings],
    }


class Toolbox:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        files_root: Path,
        payments_dir: Path,
        today: date,
        guard: Any = None,
        embedder: Embedder | None = None,
    ) -> None:
        """`guard` is an optional khata.llm.PromptGuard: a second injection check."""
        self.pool = pool
        self.guard = guard
        self.embedder = embedder or default_embedder()
        self.files_root = files_root.resolve()
        self.payments_dir = payments_dir
        self.today = today

    def as_dict(self) -> dict[str, ToolFn]:
        return {
            name: getattr(self, name)
            for name in (
                "classify_input",
                "extract_invoice",
                "validate_gst",
                "detect_injection",
                "three_way_match",
                "check_duplicate",
                "check_bank_change",
                "route_approval",
                "record_decision",
                "draft_payment",
                "get_invoice",
                "get_invoice_status",
                "search_vendor_knowledge",
                "propose_rule_change",
            )
        }

    async def _record_check(
        self, invoice_id: int, agent: str, r: CheckReport
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn, conn.transaction():
            if r.flags:
                await repo.add_flags(conn, invoice_id, r.flags)
            await audit(
                conn,
                invoice_id,
                agent,
                "agent",
                r.check,
                "; ".join(f.message for f in r.findings),
                _report(r),
            )
        return _report(r)

    # --- Dispatcher ------------------------------------------------------------

    async def classify_input(
        self,
        *,
        agent: str,
        text: str = "",
        file_name: str | None = None,
        in_invoice_thread: bool = False,
    ) -> dict[str, Any]:
        """What a room event is: an invoice, a decision, a status question, or other.

        Rules, not a model: an attachment is classified by its type, and a decision
        must say approve/reject explicitly (an LLM never turns chat into a decision).
        """
        if file_name:
            if file_name.lower().endswith(".pdf"):
                return {"kind": "invoice", "reason": "PDF attachment"}
            return {"kind": "unsupported_file", "reason": f"not a PDF: {file_name}"}
        decision = parse_decision(text)
        if decision and in_invoice_thread:
            return {"kind": "decision", **decision}
        if re.search(r"(?i)\b(status|where is|paid yet|when will)\b", text):
            return {"kind": "status_query", "reason": "asks about an invoice"}
        return {"kind": "other", "reason": "no rule matched"}

    # --- Intake ----------------------------------------------------------------

    async def extract_invoice(self, *, agent: str, file_path: str) -> dict[str, Any]:
        """Read a PDF's text. The text is untrusted data from outside the company."""
        path = Path(file_path).resolve()
        if not path.is_relative_to(self.files_root):
            raise PermissionError(f"{file_path} is outside {self.files_root}")
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            pages = len(pdf.pages)
        return {
            "untrusted": True,
            "pages": pages,
            "text": text,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    async def validate_gst(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """GSTIN format and checksum, GST type, CGST/SGST split and arithmetic."""
        async with self.pool.connection() as conn:
            inv = await repo.load_invoice_data(conn, invoice_id)
            company = await repo.get_company(conn)
            policy = await repo.get_policy(conn)
        return await self._record_check(
            invoice_id, agent, gst_check(inv, company, policy.arithmetic_tolerance)
        )

    async def detect_injection(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """Look for text in the document aimed at automated systems."""
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT raw_text FROM invoices WHERE id = %s", (invoice_id,)
                )
            ).fetchone()
        if row is None or row["raw_text"] is None:
            raise LookupError(f"invoice {invoice_id} has no document text")
        r = injection_check(row["raw_text"])
        if self.guard is not None:
            async with self.pool.connection() as conn:
                threshold = (await repo.get_policy(conn)).injection_score_threshold
            try:
                score = await self.guard.score(row["raw_text"])
            except Exception as e:  # the regex layer still stands
                logger.warning("prompt guard unavailable: %s", e)
                r.add(
                    None, f"Prompt Guard unavailable ({type(e).__name__}); regex only"
                )
            else:
                if score >= threshold:
                    r.add(
                        Flag.INJECTION,
                        f"Prompt Guard scores the text {score:.3f} "
                        f"(threshold {threshold}) as an attack on an AI",
                        score=round(score, 4),
                    )
                else:
                    r.add(
                        None, f"Prompt Guard score {score:.4f}", score=round(score, 4)
                    )
        return await self._record_check(invoice_id, agent, r)

    # --- Verifier --------------------------------------------------------------

    async def _vendor_and_data(self, conn, invoice_id: int):
        inv = await repo.load_invoice_data(conn, invoice_id)
        row = await (
            await conn.execute(
                "SELECT vendor_id FROM invoices WHERE id = %s", (invoice_id,)
            )
        ).fetchone()
        vendor = (
            await repo.get_vendor(conn, row["vendor_id"]) if row["vendor_id"] else None
        )
        return inv, vendor

    async def three_way_match(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """Invoice vs PO prices vs quantities received."""
        async with self.pool.connection() as conn:
            inv, vendor = await self._vendor_and_data(conn, invoice_id)
            po = await repo.get_po(conn, inv.po_number)
            policy = await repo.get_policy(conn)
        r = match_check(inv, po, policy.variance_pct)
        if po is not None and vendor is not None and po.vendor_id != vendor.id:
            r.add(
                Flag.PO_VENDOR_MISMATCH,
                f"{po.po_number} was raised on a different vendor",
            )
        return await self._record_check(invoice_id, agent, r)

    async def check_duplicate(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """Duplicate / possible duplicate, and whether this is a first-time vendor."""
        async with self.pool.connection() as conn:
            inv, vendor = await self._vendor_and_data(conn, invoice_id)
            policy = await repo.get_policy(conn)
            if vendor is None:
                r = CheckReport(check="check_duplicate")
                r.add(None, "Vendor unknown: nothing to compare against")
                return await self._record_check(invoice_id, agent, r)
            prior = await repo.prior_invoices(conn, vendor.id, invoice_id)
            history = await repo.has_approved_history(conn, vendor.id, invoice_id)
        r = dup_check(inv, prior, policy.duplicate_window_days)
        if not history:
            r.add(Flag.FIRST_TIME_VENDOR, f"No approved invoice from {vendor.name} yet")
        return await self._record_check(invoice_id, agent, r)

    async def check_bank_change(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """Bank details on the invoice vs the vendor master."""
        async with self.pool.connection() as conn:
            inv, vendor = await self._vendor_and_data(conn, invoice_id)
        if vendor is None:
            r = CheckReport(check="check_bank_change")
            r.add(None, "Vendor unknown: no master bank details to compare")
            return await self._record_check(invoice_id, agent, r)
        return await self._record_check(invoice_id, agent, bank_check(inv, vendor))

    # --- Router ----------------------------------------------------------------

    async def route_approval(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """Apply the active approval policy to the invoice's flags and total, and set its gate.

        Idempotent: an invoice already past the gate just gets its stored route back.
        """
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT state, flags, total, vendor_id FROM invoices WHERE id = %s FOR UPDATE",
                    (invoice_id,),
                )
            ).fetchone()
            if row is None:
                raise LookupError(f"invoice {invoice_id} not found")
            policy = await repo.get_policy(conn)
            vendor = (
                await repo.get_vendor(conn, row["vendor_id"])
                if row["vendor_id"]
                else None
            )
            flags = [Flag(f) for f in row["flags"]]
            pct = (
                await steward.price_variance_pct(conn, invoice_id)
                if Flag.PRICE_VARIANCE in flags
                else None
            )
            decision = apply_policy(flags, row["total"], policy, vendor, pct)
            state = State(row["state"])
            if state in {
                State.AWAITING_APPROVAL,
                State.ON_HOLD,
                State.APPROVED,
                State.PAYMENT_DRAFTED,
                State.REJECTED,
            }:
                return {
                    **decision.model_dump(mode="json"),
                    "applied": False,
                    "state": state.value,
                }
            await conn.execute(
                """
                UPDATE invoices SET route = %s, approval_steps = %s, approvers = %s,
                                    callback_instruction = %s WHERE id = %s""",
                (
                    decision.route,
                    decision.steps,
                    decision.approvers,
                    decision.callback_instruction,
                    invoice_id,
                ),
            )
            await transition(
                conn,
                invoice_id,
                decision.gate_state,
                agent,
                "agent",
                "; ".join(decision.reasons),
                decision.model_dump(mode="json"),
            )
            if decision.gate_state == State.APPROVED:
                await repo.insert_decision(
                    conn,
                    invoice_id,
                    "policy",
                    "policy",
                    "auto_approve",
                    None,
                    "; ".join(decision.reasons),
                    policy.version,
                    None,
                )
        return {
            **decision.model_dump(mode="json"),
            "applied": True,
            "state": decision.gate_state.value,
        }

    async def record_decision(
        self,
        *,
        agent: str,
        invoice_id: int,
        username: str,
        decision: str,
        reason: str,
        switch_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a person's approve/reject from the room. Khata checks they may make it.

        The next approval step decides who may approve; anyone on the route may
        reject. A clerk can do neither. Every decision needs a reason.
        """
        decision = decision.strip().lower()
        if decision not in {"approve", "reject"}:
            raise DecisionRefused(
                f"decision must be approve or reject, got {decision!r}"
            )
        if len(reason.strip()) < 3:
            raise DecisionRefused("a decision needs a reason")
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT state, approval_steps, total FROM invoices WHERE id = %s FOR UPDATE",
                    (invoice_id,),
                )
            ).fetchone()
            if row is None:
                raise LookupError(f"invoice {invoice_id} not found")
            state = State(row["state"])
            if state not in {State.AWAITING_APPROVAL, State.ON_HOLD}:
                raise DecisionRefused(
                    f"invoice {invoice_id} is {state.value}; nothing to decide"
                )
            who = await repo.person(conn, username)
            if who is None:
                raise DecisionRefused(f"{username} is not in the finance team")
            steps: list[str] = row["approval_steps"]
            done = [
                d["approval_step"]
                for d in await repo.decisions_for(conn, invoice_id)
                if d["decision"] == "approve"
            ]
            pending = [s for s in steps if s not in done]
            if not pending:
                raise DecisionRefused(
                    f"invoice {invoice_id} has no approval step pending"
                )
            step = pending[0]
            roles_on_route = {ROLE_FOR_STEP[s] for s in steps}
            if decision == "approve" and who["role"] != ROLE_FOR_STEP[step]:
                raise DecisionRefused(
                    f"{who['display_name']} ({who['role']}) can't approve the {step} step"
                )
            if decision == "reject" and who["role"] not in roles_on_route:
                raise DecisionRefused(
                    f"{who['display_name']} ({who['role']}) isn't an approver here"
                )
            policy = await repo.get_policy(conn)
            await repo.insert_decision(
                conn,
                invoice_id,
                who["username"],
                "human",
                decision,
                step,
                reason.strip(),
                policy.version,
                switch_message_id,
            )
            if decision == "reject":
                new = State.REJECTED
            else:
                new = State.AWAITING_APPROVAL if len(pending) > 1 else State.APPROVED
            await transition(
                conn,
                invoice_id,
                new,
                who["username"],
                "human",
                f"{decision} ({step}): {reason.strip()}",
                {"via": agent, "switch_message_id": switch_message_id},
            )
        remaining = pending[1:] if decision == "approve" else []
        return {
            "invoice_id": invoice_id,
            "state": new.value,
            "step": step,
            "decided_by": who["username"],
            "remaining_steps": remaining,
        }

    # --- Payer -----------------------------------------------------------------

    async def draft_payment(
        self, *, agent: str, invoice_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """Draft a NEFT bulk-upload CSV + journal for APPROVED invoices. Never pays. Idempotent."""
        async with self.pool.connection() as conn, conn.transaction():
            ids = invoice_ids or await repo.invoices_in_state(conn, State.APPROVED)
            if not ids:
                return {
                    "batch_ref": None,
                    "drafted": [],
                    "already_drafted": [],
                    "total": "0.00",
                }
            try:
                res = await draft(conn, ids, agent, self.payments_dir, self.today)
            except NotPayable as e:
                raise DecisionRefused(str(e)) from e
        out = asdict(res)
        out["total"] = f"{res.total:.2f}"
        return out

    # --- Policy Steward -----------------------------------------------------------

    async def propose_rule_change(
        self, *, agent: str, invoice_id: int
    ) -> dict[str, Any]:
        """After a person approves an invoice, look for a repeated exception and propose
        a rule for the owner. Proposes only; never changes the policy."""
        async with self.pool.connection() as conn, conn.transaction():
            return await steward.propose(conn, invoice_id)

    # --- Reading ---------------------------------------------------------------

    async def get_invoice(self, *, agent: str, invoice_id: int) -> dict[str, Any]:
        """The invoice as Khata has it: fields, lines, flags, decisions and history."""
        async with self.pool.connection() as conn:
            return await repo.get_invoice(conn, invoice_id)

    async def get_invoice_status(
        self, *, agent: str, vendor_code: str, invoice_no: str | None = None
    ) -> dict[str, Any]:
        """Where a vendor's invoice is in the process (for the vendor liaison).
        Without a number: that vendor's five most recent invoices."""
        async with self.pool.connection() as conn:
            rows = await repo.invoice_status(conn, vendor_code, invoice_no)
        return {"vendor_code": vendor_code, "invoice_no": invoice_no, "matches": rows}

    async def search_vendor_knowledge(
        self, *, agent: str, vendor_code: str, query: str = ""
    ) -> dict[str, Any]:
        """What the team knows about a vendor: notes, agreed rules and past decisions with
        reasons, nearest the query first (local embeddings + pgvector)."""
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT id, name FROM vendors WHERE code = %s",
                    (vendor_code.upper(),),
                )
            ).fetchone()
            if row is None:
                return {"vendor_code": vendor_code, "items": [], "retrieval": None}
            await repo.sync_decision_knowledge(conn, row["id"])
            try:
                todo = await repo.knowledge_to_embed(
                    conn, row["id"], self.embedder.name
                )
                if todo:
                    vecs = await self.embedder.embed([t["content"] for t in todo])
                    for t, v in zip(todo, vecs, strict=True):
                        await repo.set_embedding(
                            conn, t["id"], to_pgvector(v), self.embedder.name
                        )
                if query:
                    (qv,) = await self.embedder.embed([query])
                    items = await repo.vendor_knowledge_nearest(
                        conn, row["id"], to_pgvector(qv), 10
                    )
                else:
                    items = (await repo.vendor_knowledge(conn, row["id"], None))[:10]
                retrieval = f"pgvector cosine over {self.embedder.name}"
            except Exception as e:  # the model can't load: say so, rank by keywords
                logger.warning("embeddings unavailable, keyword ranking: %s", e)
                items = (await repo.vendor_knowledge(conn, row["id"], query or None))[
                    :10
                ]
                retrieval = (
                    f"keyword overlap (embeddings unavailable: {type(e).__name__})"
                )
        for it in items:
            if "score" in it:
                it["score"] = float(it["score"])
        return {
            "vendor_code": vendor_code,
            "vendor": row["name"],
            "retrieval": retrieval,
            "items": items,
        }


# --- Code-node helpers (called by the workflow itself, not offered to agents) --------


async def ingest(
    pool: AsyncConnectionPool,
    *,
    file_path: str,
    text: str,
    sha256: str,
    room_id: str | None,
    thread_id: str | None,
    graph_thread_id: str | None = None,
) -> int:
    """Create the invoice row for a received document."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await repo.create_invoice(
            conn, file_path, sha256, room_id, thread_id
        )
        await conn.execute(
            "UPDATE invoices SET raw_text = %s, graph_thread_id = %s WHERE id = %s",
            (text, graph_thread_id, invoice_id),
        )
        await audit(
            conn,
            invoice_id,
            "system",
            "system",
            "received",
            file_path,
            to_state=State.RECEIVED,
        )
    return invoice_id


async def save_extraction(
    pool: AsyncConnectionPool,
    *,
    invoice_id: int,
    data: InvoiceData,
    readings: dict[str, Any],
    confidence,
    actor: str,
    extra_flags: list[Flag] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Store the agreed reading, identify the vendor, and move to EXTRACTED."""
    async with pool.connection() as conn, conn.transaction():
        vendor, matched_by = await repo.find_vendor(
            conn, data.supplier_gstin, data.vendor_name
        )
        await repo.store_extraction(
            conn, invoice_id, data, vendor, readings, confidence
        )
        flags = list(extra_flags or [])
        if vendor is None:
            flags.append(Flag.VENDOR_UNKNOWN)
        elif matched_by == "name" and data.supplier_gstin != vendor.gstin:
            # A malformed or wrong-checksum GSTIN is GSTIN_INVALID (validate_gst);
            # a *valid* GSTIN that isn't the master's is a mismatch to look at.
            if is_valid_gstin(data.supplier_gstin):
                flags.append(Flag.GSTIN_MISMATCH)
        if flags:
            await repo.add_flags(conn, invoice_id, flags)
        await transition(
            conn,
            invoice_id,
            State.EXTRACTED,
            actor,
            "agent",
            f"Read invoice {data.invoice_no} from "
            f"{vendor.name if vendor else data.vendor_name}",
            {
                "vendor_matched_by": matched_by,
                "flags": [f.value for f in flags],
                "notes": notes or [],
            },
        )
    return {
        "vendor_code": vendor.code if vendor else None,
        "matched_by": matched_by,
        "flags": [f.value for f in flags],
    }


async def save_note(
    pool: AsyncConnectionPool,
    *,
    invoice_id: int,
    agent: str,
    kind: str,
    content: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Keep what an LLM agent wrote, and note it in the audit trail."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO agent_notes (invoice_id, agent, kind, content, provider, model)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                invoice_id,
                agent,
                kind,
                json.dumps(content, default=str),
                provider,
                model,
            ),
        )
        await audit(
            conn,
            invoice_id,
            agent,
            "agent",
            kind,
            str(content.get("headline") or content.get("summary") or kind)[:500],
            {"provider": provider, "model": model},
        )


_DECISION = re.compile(
    r"^\s*(?:@\S+\s+)?(?P<d>approve[ds]?|reject(?:ed)?)\b[\s:,.-]*(?P<reason>.*)$",
    re.I | re.S,
)


def parse_decision(text: str) -> dict[str, str] | None:
    """'approve - freight agreed' -> {decision: approve, reason: 'freight agreed'}.

    The message must *start* with approve or reject (after an optional @mention).
    "Looks fine I guess" is not a decision; "don't approve" isn't either.
    """
    m = _DECISION.match(text or "")
    if not m:
        return None
    word = m.group("d").lower()
    return {
        "decision": "approve" if word.startswith("approve") else "reject",
        "reason": m.group("reason").strip(),
    }
