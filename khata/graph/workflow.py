"""The invoice workflow as a LangGraph graph, checkpointed in Postgres.

    dispatch -> ingest -> read (2 models, in parallel) -> reconcile
      -> validate -> [three_way_match | check_duplicate | check_bank_change] -> reviewed
      -> route -> (auto) pay | (reject) END | (person) analyst -> auditor -> await_human
    await_human -> interrupt() ... a person replies ... -> record_decision
      -> await_human again (next step, or a refused reply) | pay | END

One graph run per invoice; its thread id is stored on the invoice row. The
approval gate is LangGraph's `interrupt`: the run stops, its state is in
Postgres, and any later process can resume it with the person's reply.

Every tool call goes through the ToolGateway as the agent that owns it. Nodes
are written to be safe to re-run (a crash between a database write and the
checkpoint replays the node): each checks the invoice's state first.
"""

from __future__ import annotations

import asyncio
import json
import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt
from psycopg_pool import AsyncConnectionPool

from khata.agents.intake import Reading, read_invoice, reconcile
from khata.agents.review import Review, analyse, audit_review
from khata.agents.schemas import AnalystNote
from khata.approval import REJECT
from khata.graph import messages as msg
from khata.llm import LLM, LLMUnavailable
from khata.models import Flag, State
from khata.policy import ToolGateway
from khata.repo import get_policy
from khata.state import transition
from khata.tools import DecisionRefused, ingest, save_extraction, save_note

REJECT_FLAGS = {f.value for f in REJECT}
GATE = {"AWAITING_APPROVAL", "ON_HOLD", "APPROVED", "REJECTED", "PAYMENT_DRAFTED"}
ORDER = [
    "RECEIVED",
    "EXTRACTED",
    "VALIDATED",
    "MATCHED",
    "REVIEWED",
]


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


def _j(value: Any) -> Any:
    """Plain JSON for the checkpointed state."""
    return json.loads(json.dumps(value, default=str))


class InvoiceState(TypedDict, total=False):
    file_path: str
    room_id: str | None
    thread_id: str | None  # the Switch thread (Phase 4)
    kind: str
    invoice_id: int
    readings: dict[str, Any]
    extraction: dict[str, Any]
    reports: Annotated[dict[str, Any], _merge]
    route: dict[str, Any]
    review: dict[str, Any]
    decision_error: str | None
    last_decision: dict[str, Any] | None
    payment: dict[str, Any]
    state: str
    log: Annotated[list[dict[str, str]], operator.add]


class ExtractionFailed(RuntimeError):
    """Neither reader produced a usable transcription. Resume the run to retry."""


@dataclass
class Deps:
    pool: AsyncConnectionPool
    gw: ToolGateway
    llm: LLM


def say(agent: str, text: str) -> list[dict[str, str]]:
    return [{"agent": agent, "text": text}]


def build_graph(deps: Deps) -> StateGraph:
    pool, gw, llm = deps.pool, deps.gw, deps.llm

    async def db_state(invoice_id: int) -> str:
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT state FROM invoices WHERE id = %s", (invoice_id,)
                )
            ).fetchone()
        return row["state"]

    async def advance(invoice_id: int, to: State, actor: str, reason: str) -> None:
        async with pool.connection() as conn, conn.transaction():
            await transition(conn, invoice_id, to, actor, "agent", reason)

    def past(state: str, target: str) -> bool:
        """Has the invoice already reached `target` (or the gate)?"""
        return state in GATE or ORDER.index(state) >= ORDER.index(target)

    # -- Dispatcher ------------------------------------------------------------

    async def dispatch(s: InvoiceState) -> dict:
        res = await gw.call("dispatcher", "classify_input", file_name=s["file_path"])
        return {
            "kind": res["kind"],
            **(
                {}
                if res["kind"] == "invoice"
                else {
                    "log": say("khata-dispatcher", f"Not an invoice: {res['reason']}.")
                }
            ),
        }

    # -- Intake ----------------------------------------------------------------

    async def ingest_node(s: InvoiceState, config) -> dict:
        graph_thread = config["configurable"]["thread_id"]
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id FROM invoices WHERE graph_thread_id = %s",
                    (graph_thread,),
                )
            ).fetchone()
        if row:  # a replay after a crash: the row already exists
            return {"invoice_id": row["id"]}
        doc = await gw.call("intake", "extract_invoice", file_path=s["file_path"])
        invoice_id = await ingest(
            pool,
            file_path=s["file_path"],
            text=doc["text"],
            sha256=doc["sha256"],
            room_id=s.get("room_id"),
            thread_id=s.get("thread_id"),
            graph_thread_id=graph_thread,
        )
        return {
            "invoice_id": invoice_id,
            "log": say(
                "khata-intake",
                f"Received {s['file_path'].rsplit('/', 1)[-1]} "
                f"({doc['pages']} page{'s' if doc['pages'] != 1 else ''}).",
            ),
        }

    async def read(s: InvoiceState) -> dict:
        """Two independent readers, at the same time."""
        inv = s["invoice_id"]
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT raw_text FROM invoices WHERE id = %s", (inv,)
                )
            ).fetchone()
        a, b = await asyncio.gather(
            read_invoice(llm, "a", row["raw_text"], inv),
            read_invoice(llm, "b", row["raw_text"], inv),
        )
        if a.data is None and b.data is None:
            # Failing the node keeps it pending in the checkpoint: resuming retries it.
            raise ExtractionFailed(f"a: {a.error}; b: {b.error}")
        return {"readings": {"a": a.dump(), "b": b.dump()}}

    async def reconcile_node(s: InvoiceState) -> dict:
        inv = s["invoice_id"]
        a = Reading.load(s["readings"]["a"])
        b = Reading.load(s["readings"]["b"])
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT raw_text, state FROM invoices WHERE id = %s", (inv,)
                )
            ).fetchone()
            policy = await get_policy(conn)
        rec = reconcile(a, b, row["raw_text"], policy.min_extraction_confidence)
        record = rec.readings_record(a, b)
        if row["state"] == "RECEIVED":
            saved = await save_extraction(
                pool,
                invoice_id=inv,
                data=rec.data,
                readings=record,
                confidence=rec.confidence,
                actor="khata-intake",
                extra_flags=rec.flags,
                notes=rec.notes,
            )
            await save_note(
                pool,
                invoice_id=inv,
                agent="khata-intake",
                kind="reading",
                content={"summary": "two readings compared", **record},
                provider=",".join(filter(None, (a.endpoint, b.endpoint))),
                model=",".join(filter(None, (a.model, b.model))),
            )
        else:
            saved = {"flags": [f.value for f in rec.flags]}
        info = await gw.call("analyst", "get_invoice", invoice_id=inv)
        return {
            "extraction": _j({**record, "vendor_flags": saved["flags"]}),
            "log": say("khata-intake", msg.extraction_line(info, record)),
        }

    def after_reconcile(s: InvoiceState) -> str:
        # Unconfirmed data isn't worth checking: a person looks at both readings.
        if Flag.EXTRACTION_DISAGREEMENT.value in s["extraction"]["flags"]:
            return "route"
        return "validate"

    async def validate(s: InvoiceState) -> dict:
        inv = s["invoice_id"]
        if past(await db_state(inv), "VALIDATED"):
            return {}
        gst = await gw.call("intake", "validate_gst", invoice_id=inv)
        inj = await gw.call("intake", "detect_injection", invoice_id=inv)
        reports = {"validate_gst": gst, "detect_injection": inj}
        if not set(gst["flags"]) & REJECT_FLAGS:
            await advance(
                inv, State.VALIDATED, "khata-intake", "GST and document checks done"
            )
        return {"reports": _j(reports)}

    def after_validate(s: InvoiceState) -> str | list[str]:
        if (
            set(s.get("reports", {}).get("validate_gst", {}).get("flags", []))
            & REJECT_FLAGS
        ):
            return "route"
        return ["three_way_match", "check_duplicate", "check_bank_change"]

    # -- Verifier: three independent checks, in parallel ------------------------

    def verifier(tool: str):
        async def node(s: InvoiceState) -> dict:
            inv = s["invoice_id"]
            if past(await db_state(inv), "MATCHED"):
                return {}
            return {
                "reports": {tool: _j(await gw.call("verifier", tool, invoice_id=inv))}
            }

        return node

    async def reviewed(s: InvoiceState) -> dict:
        inv = s["invoice_id"]
        if not past(await db_state(inv), "MATCHED"):
            await advance(
                inv,
                State.MATCHED,
                "khata-verifier",
                "Match, duplicate and bank checks done",
            )
        if not past(await db_state(inv), "REVIEWED"):
            await advance(inv, State.REVIEWED, "khata-verifier", "Ready for routing")
        return {"log": say("khata-verifier", msg.checks_line(s.get("reports", {})))}

    # -- Router ------------------------------------------------------------------

    async def route(s: InvoiceState) -> dict:
        r = await gw.call("router", "route_approval", invoice_id=s["invoice_id"])
        return {
            "route": _j(r),
            "state": r["state"],
            "log": say("khata-router", msg.route_line(r)),
        }

    def after_route(s: InvoiceState) -> str:
        return {"APPROVED": "pay", "REJECTED": "done"}.get(
            s["route"]["state"], "analyst"
        )

    # -- Analyst and Auditor -------------------------------------------------------

    async def analyst(s: InvoiceState) -> dict:
        inv = s["invoice_id"]
        review, _ = await analyse(llm, gw, inv)
        out: dict[str, Any] = {
            "analyst": review.note.model_dump(mode="json") if review.note else None,
            "analyst_model": review.analyst_model,
            "analyst_provider": review.analyst_provider,
            "corrections": review.corrections,
            "errors": review.errors,
        }
        if review.note:
            await save_note(
                pool,
                invoice_id=inv,
                agent="khata-analyst",
                kind="analysis",
                content={**out["analyst"], "corrections": review.corrections},
                provider=review.analyst_provider,
                model=review.analyst_model,
            )
        text = (
            f"{review.note.headline} {review.note.explanation}"
            if review.note
            else "No analysis available (" + "; ".join(review.errors) + ")."
        )
        return {"review": _j(out), "log": say("khata-analyst", text)}

    async def auditor(s: InvoiceState) -> dict:
        inv = s["invoice_id"]
        prev = s["review"]
        review = Review(
            note=AnalystNote.model_validate(prev["analyst"])
            if prev["analyst"]
            else None,
            analyst_model=prev["analyst_model"],
            analyst_provider=prev["analyst_provider"],
            corrections=prev["corrections"],
            errors=list(prev["errors"]),
        )
        review = await audit_review(llm, gw, inv, review)
        verdict = review.verdict.model_dump(mode="json") if review.verdict else None
        if verdict:
            await save_note(
                pool,
                invoice_id=inv,
                agent="khata-auditor",
                kind="audit",
                content={
                    "summary": "consistent"
                    if verdict["consistent"]
                    else "issues found",
                    **verdict,
                },
                provider=review.auditor_provider,
                model=review.auditor_model,
            )
        text = (
            "No audit available."
            if verdict is None
            else "Agrees with the Analyst's note."
            if verdict["consistent"]
            else "Disagrees with the Analyst: " + "; ".join(verdict["issues"])
        )
        return {
            "review": _j(
                {
                    **prev,
                    "audit": verdict,
                    "auditor_model": review.auditor_model,
                    "auditor_provider": review.auditor_provider,
                    "errors": review.errors,
                }
            ),
            "log": say("khata-auditor", text),
        }

    # -- The approval gate -----------------------------------------------------------

    async def approval_request(s: InvoiceState) -> dict[str, Any]:
        inv = await gw.call("analyst", "get_invoice", invoice_id=s["invoice_id"])
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT approval_steps, approvers, callback_instruction, route "
                    "FROM invoices WHERE id = %s",
                    (s["invoice_id"],),
                )
            ).fetchone()
        done = [
            d["approval_step"] for d in inv["decisions"] if d["decision"] == "approve"
        ]
        pending = [st for st in row["approval_steps"] if st not in done]
        approver = dict(zip(row["approval_steps"], row["approvers"], strict=False))
        review = s.get("review") or {}
        req = {
            "invoice_id": s["invoice_id"],
            "invoice_no": inv["invoice_no"],
            "vendor_name": inv["vendor_name"],
            "total": str(inv["total"]),
            "state": inv["state"],
            "flags": inv["flags"],
            "route": row["route"],
            "next_step": pending[0] if pending else None,
            "next_approver": approver.get(pending[0]) if pending else None,
            "pending_steps": pending,
            "hard_stop": bool(row["callback_instruction"]),
            "callback_instruction": row["callback_instruction"],
            "analyst": review.get("analyst"),
            "audit": review.get("audit"),
            "error": s.get("decision_error"),
        }
        req["message"] = msg.approval_message(req)
        return _j(req)

    async def await_human(s: InvoiceState) -> dict:
        # Everything before interrupt() re-runs on resume, so it only reads.
        request = await approval_request(s)
        reply = interrupt(request)
        try:
            res = await gw.call(
                "router",
                "record_decision",
                invoice_id=s["invoice_id"],
                username=reply["username"],
                decision=reply["decision"],
                reason=reply.get("reason", ""),
                switch_message_id=reply.get("message_id"),
            )
        except DecisionRefused as e:
            return {
                "decision_error": str(e),
                "log": say("khata-router", f"Not recorded: {e}"),
            }
        text = (
            f"{res['decided_by']} {'approved' if reply['decision'] == 'approve' else 'rejected'} the {res['step']} step: "
            f"{reply.get('reason', '').strip()}"
        )
        if res["remaining_steps"]:
            text += f" Next: {', '.join(res['remaining_steps'])}."
        return {
            "decision_error": None,
            "last_decision": _j(res),
            "state": res["state"],
            "log": say("khata-router", text),
        }

    async def after_human(s: InvoiceState) -> str:
        state = await db_state(s["invoice_id"])
        return {"APPROVED": "pay", "REJECTED": "done"}.get(state, "await_human")

    # -- Payer ---------------------------------------------------------------------------

    async def pay(s: InvoiceState) -> dict:
        res = await gw.call("payer", "draft_payment", invoice_ids=[s["invoice_id"]])
        if res["drafted"]:
            text = (
                f"Drafted NEFT batch {res['batch_ref']} for ₹{res['total']} to the vendor's "
                "account on file, with journal entries. Nothing has been paid: upload it to "
                "the bank yourself."
            )
        else:
            text = "Already drafted earlier; nothing new."
        return {
            "payment": _j(res),
            "state": "PAYMENT_DRAFTED",
            "log": say("khata-payer", text),
        }

    async def done(s: InvoiceState) -> dict:
        return {"state": await db_state(s["invoice_id"])}

    # -- Wiring -------------------------------------------------------------------------

    llm_retry = RetryPolicy(max_attempts=3, retry_on=(LLMUnavailable, ExtractionFailed))
    g = StateGraph(InvoiceState)
    g.add_node("dispatch", dispatch)
    g.add_node("ingest", ingest_node)
    g.add_node("read", read, retry_policy=llm_retry)
    g.add_node("reconcile", reconcile_node)
    g.add_node("validate", validate)
    for tool in ("three_way_match", "check_duplicate", "check_bank_change"):
        g.add_node(tool, verifier(tool))
    g.add_node("reviewed", reviewed)
    g.add_node("route", route)
    g.add_node("analyst", analyst)
    g.add_node("auditor", auditor)
    g.add_node("await_human", await_human)
    g.add_node("pay", pay)
    g.add_node("done", done)

    g.add_edge(START, "dispatch")
    g.add_conditional_edges(
        "dispatch",
        lambda s: "ingest" if s["kind"] == "invoice" else END,
        ["ingest", END],
    )
    g.add_edge("ingest", "read")
    g.add_edge("read", "reconcile")
    g.add_conditional_edges("reconcile", after_reconcile, ["validate", "route"])
    g.add_conditional_edges(
        "validate",
        after_validate,
        ["route", "three_way_match", "check_duplicate", "check_bank_change"],
    )
    g.add_edge(["three_way_match", "check_duplicate", "check_bank_change"], "reviewed")
    g.add_edge("reviewed", "route")
    g.add_conditional_edges("route", after_route, ["pay", "done", "analyst"])
    g.add_edge("analyst", "auditor")
    g.add_edge("auditor", "await_human")
    g.add_conditional_edges("await_human", after_human, ["pay", "done", "await_human"])
    g.add_edge("pay", END)
    g.add_edge("done", END)
    return g
