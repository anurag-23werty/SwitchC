"""Khata's records, written as Switch room documents (Postgres -> room).

Switch has no Rules/Decisions/Knowledge objects; room-scoped documents are the
closest thing, and every member of the room can read them. Postgres stays the
system of record; these are regenerated from it. Only the creating agent can
update a room document, so the Router owns all three.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection

from khata import repo
from khata.models import Policy
from khata.money import inr

MAX_DECISIONS = 200  # keeps the document well under Switch's 1 MB limit


def _rs(v: Any) -> str:
    return "₹" + inr(Decimal(str(v)))


def policy_text(p: Policy) -> str:
    return "\n".join(
        [
            f"Approval policy v{p.version}",
            f"- Clean invoices under {_rs(p.auto_approve_below)}: approved automatically.",
            f"- {_rs(p.auto_approve_below)} to {_rs(p.manager_up_to)}, a price/quantity "
            f"variance over {p.variance_pct}%, or a first-time vendor: finance manager "
            f"(@{p.approvers['finance_manager']}).",
            f"- Over {_rs(p.manager_up_to)}: finance manager, then owner "
            f"(@{p.approvers['owner']}).",
            "- Bank details different from the vendor master: HARD STOP for the owner, "
            "who calls the vendor on the number on file.",
            "- Text aimed at AI agents, possible duplicates, unknown vendors or POs, and "
            "unconfirmed readings: on hold for the finance manager.",
            "- Invalid GSTIN, wrong GST type, bad arithmetic or a duplicate number: "
            "rejected; the vendor must reissue.",
            f"- Arithmetic tolerance {_rs(p.arithmetic_tolerance)}; duplicate window "
            f"{p.duplicate_window_days} days.",
            "- No agent ever pays. Payer drafts a NEFT file for a person to upload.",
            *[
                f"- Vendor exception: {e.vendor_code} price variance up to "
                f"{e.max_variance_pct}% over the PO is accepted (proposed by the Policy "
                f"Steward after {', '.join(e.evidence)}; approved by {e.approved_by})."
                for e in p.vendor_exceptions
            ],
        ]
    )


async def policy_doc(conn: AsyncConnection) -> str:
    p = await repo.get_policy(conn)
    rows = await (
        await conn.execute(
            "SELECT version, change_summary, proposed_by, approved_by, active, created_at "
            "FROM rules ORDER BY version"
        )
    ).fetchall()
    history = [
        f"- v{r['version']}{' (active)' if r['active'] else ''}: {r['change_summary']} "
        f"(proposed by {r['proposed_by']}, approved by {r['approved_by']}, "
        f"{r['created_at']:%Y-%m-%d})"
        for r in rows
    ]
    return policy_text(p) + "\n\nVersions\n" + "\n".join(history)


async def decisions_doc(conn: AsyncConnection) -> str:
    rows = await (
        await conn.execute(
            """
            SELECT d.created_at, d.decided_by, d.decider_kind, d.decision, d.approval_step,
                   d.reason, d.rule_version, i.invoice_no, i.total, v.code AS vendor
            FROM decisions d JOIN invoices i ON i.id = d.invoice_id
            LEFT JOIN vendors v ON v.id = i.vendor_id
            ORDER BY d.id DESC LIMIT %s""",
            (MAX_DECISIONS,),
        )
    ).fetchall()
    lines = [
        f"- {r['created_at']:%Y-%m-%d %H:%M} {r['vendor'] or '?'} {r['invoice_no']} "
        f"{_rs(r['total'])}: {r['decision']}"
        f"{' (' + r['approval_step'] + ')' if r['approval_step'] else ''} by "
        f"{r['decided_by']} under policy v{r['rule_version']}: {r['reason']}"
        for r in rows
    ]
    return "Decisions, newest first (from Khata's decisions table)\n" + (
        "\n".join(lines) or "- none yet"
    )


async def knowledge_doc(conn: AsyncConnection) -> str:
    vendors = await (
        await conn.execute("SELECT id, code, name FROM vendors ORDER BY code")
    ).fetchall()
    out = ["What the team knows about each vendor (notes and past decisions)"]
    for v in vendors:
        items = await repo.vendor_knowledge(conn, v["id"], None)
        out.append(f"\n{v['code']}: {v['name']}")
        out += [
            f"- {i['text']}" + (f" [{i['invoice_no']}]" if i["invoice_no"] else "")
            for i in items[:15]
        ] or ["- nothing yet"]
    return "\n".join(out)


DOCS = {
    "khata-approval-policy": (
        "The approval policy in force, and its versions",
        "Read before saying who approves what. Generated from Khata's rules table.",
        policy_doc,
    ),
    "khata-decisions-log": (
        "Every approval and rejection, with reasons",
        "Generated from Khata's decisions table after each decision.",
        decisions_doc,
    ),
    "khata-vendor-knowledge": (
        "Vendor notes and past decisions",
        "Generated from Khata's vendor knowledge and decisions.",
        knowledge_doc,
    ),
}
