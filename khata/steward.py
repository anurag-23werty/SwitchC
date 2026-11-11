"""Policy Steward: notices when people keep approving the same exception, and
proposes a rule for it. The owner decides; Khata never changes a rule by itself.

    a person approves an invoice whose only flag is PRICE_VARIANCE
      -> count that vendor's human-approved invoices with exactly that flag
      -> at `steward.min_overrides` (2) or more: propose accepting a price variance
         up to the largest one people approved, for that vendor only
    the owner replies `approve <reason>` in the proposal's thread
      -> a new `rules` version with the vendor exception; the old version stays

All of this is code. The numbers in a proposal come from the decisions on
record, never from a model. Which flags may ever be relaxed is itself in the
policy (`steward.eligible_flags`, PRICE_VARIANCE only); bank changes, injection,
duplicates and unknown vendors can't be proposed, and the cap
(`steward.max_variance_pct`) bounds how far a variance can be accepted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection

from khata import repo
from khata.checks.matching import three_way_match
from khata.models import Flag
from khata.money import inr

STEWARD = "khata-steward"


class ProposalRefused(ValueError):
    """A rule decision Khata won't record (wrong person, already decided...)."""


@dataclass
class Evidence:
    invoice_id: int
    invoice_no: str
    total: Decimal
    variance_pct: Decimal
    approved_by: str
    reason: str


async def price_variance_pct(conn: AsyncConnection, invoice_id: int) -> Decimal | None:
    """The invoice's price variance over its PO, re-measured from the stored reading."""
    try:
        inv = await repo.load_invoice_data(conn, invoice_id)
    except LookupError:
        return None
    po = await repo.get_po(conn, inv.po_number)
    if po is None:
        return None
    policy = await repo.get_policy(conn)
    report = three_way_match(inv, po, policy.variance_pct)
    for f in report.findings:
        if f.flag == Flag.PRICE_VARIANCE:
            return Decimal(f.details["variance_pct"])
    return None


async def approved_overrides(
    conn: AsyncConnection, vendor_id: int, flag: str
) -> list[Evidence]:
    """This vendor's invoices that carried exactly `flag` and that a person approved."""
    rows = await (
        await conn.execute(
            """
            SELECT i.id, i.invoice_no, i.total,
                   string_agg(d.decided_by, ', ' ORDER BY d.id) AS approved_by,
                   string_agg(d.reason, ' / ' ORDER BY d.id) AS reason
            FROM invoices i
            JOIN decisions d ON d.invoice_id = i.id
                            AND d.decider_kind = 'human' AND d.decision = 'approve'
            WHERE i.vendor_id = %s AND i.flags = ARRAY[%s]::text[]
              AND i.state IN ('APPROVED', 'PAYMENT_DRAFTED')
            GROUP BY i.id ORDER BY i.id""",
            (vendor_id, flag),
        )
    ).fetchall()
    out = []
    for r in rows:
        pct = await price_variance_pct(conn, r["id"])
        if pct is not None:
            out.append(
                Evidence(
                    r["id"],
                    r["invoice_no"],
                    r["total"],
                    pct,
                    r["approved_by"],
                    r["reason"],
                )
            )
    return out


def _rs(v: Any) -> str:
    return "₹" + inr(Decimal(str(v)))


def proposal_message(p: dict[str, Any]) -> str:
    """The room post for a proposal. Built from the record, not written by a model."""
    lines = [
        f"**Rule proposal #{p['id']}** for @{p['owner']}",
        "",
        f"People have approved the same exception from {p['vendor_name']} "
        f"{len(p['evidence'])} times:",
    ]
    for e in p["evidence"]:
        lines.append(
            f"- {e['invoice_no']} {_rs(e['total'])}: +{e['variance_pct']}% over the PO, "
            f'approved by {e["approved_by"]}: "{e["reason"]}"'
        )
    lines += [
        "",
        f"**Proposal** (policy v{p['base_version']} → v{p['base_version'] + 1}): accept a "
        f"price variance of up to {p['max_variance_pct']}% over the PO on "
        f"{p['vendor_name']} invoices, so the variance alone no longer needs a sign-off.",
        f"- Amount limits stay: over {_rs(p['auto_approve_below'])} still goes to the "
        f"finance manager, over {_rs(p['manager_up_to'])} also to the owner.",
        "- Bank changes, duplicates, quantity differences, GST and every other check are "
        "unchanged, and a variance above the agreed percentage is flagged as before.",
        "",
        "Reply in this thread with `approve <reason>` or `reject <reason>`. Only the owner "
        "can approve a rule change.",
    ]
    return "\n".join(lines)


async def _proposal(conn: AsyncConnection, proposal_id: int) -> dict[str, Any]:
    row = await (
        await conn.execute(
            """SELECT p.*, v.name AS vendor_name, v.code AS vendor_code
               FROM rule_proposals p JOIN vendors v ON v.id = p.vendor_id
               WHERE p.id = %s""",
            (proposal_id,),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"rule proposal {proposal_id} not found")
    return dict(row)


async def describe(conn: AsyncConnection, proposal_id: int) -> dict[str, Any]:
    """A proposal with its evidence and the message to post, for people and the room."""
    p = await _proposal(conn, proposal_id)
    policy = await repo.get_policy(conn)
    ev = [
        e
        for e in await approved_overrides(conn, p["vendor_id"], p["flag"])
        if e.invoice_id in p["evidence"]
    ]
    out = {
        **{k: v for k, v in p.items() if k not in ("created_at", "decided_at")},
        "max_variance_pct": str(p["max_variance_pct"]),
        "evidence": [
            {**asdict(e), "total": str(e.total), "variance_pct": str(e.variance_pct)}
            for e in ev
        ],
        "owner": policy.approvers["owner"],
        "auto_approve_below": str(policy.auto_approve_below),
        "manager_up_to": str(policy.manager_up_to),
    }
    out["message"] = proposal_message(out)
    return out


async def propose(
    conn: AsyncConnection, invoice_id: int, proposed_by: str = STEWARD
) -> dict[str, Any]:
    """Look at the vendor behind a just-approved invoice; propose a rule if there's a pattern.

    Returns {"proposed": bool, "reason": str, "proposal": {...} | None}.
    """
    inv = await (
        await conn.execute(
            "SELECT vendor_id, flags, state FROM invoices WHERE id = %s", (invoice_id,)
        )
    ).fetchone()
    if inv is None:
        raise LookupError(f"invoice {invoice_id} not found")
    policy = await repo.get_policy(conn)
    eligible = [f for f in inv["flags"] if f in policy.steward_eligible_flags]

    def no(reason: str) -> dict[str, Any]:
        return {"proposed": False, "reason": reason, "proposal": None}

    if inv["vendor_id"] is None or inv["state"] not in ("APPROVED", "PAYMENT_DRAFTED"):
        return no("not an approved invoice from a known vendor")
    if len(inv["flags"]) != 1 or not eligible:
        return no("no single eligible exception on this invoice")
    flag = eligible[0]
    vendor = await repo.get_vendor(conn, inv["vendor_id"])
    evidence = await approved_overrides(conn, vendor.id, flag)
    if len(evidence) < policy.steward_min_overrides:
        return no(
            f"{len(evidence)} approval(s) of {flag} for {vendor.code}; "
            f"a pattern needs {policy.steward_min_overrides}"
        )
    pct = max(e.variance_pct for e in evidence)
    if pct > policy.steward_max_variance_pct:
        return no(
            f"approved variances reach {pct}%, above the "
            f"{policy.steward_max_variance_pct}% the Steward may ever propose"
        )
    exc = policy.exception_for(vendor.code, flag)
    if exc is not None and exc.max_variance_pct >= pct:
        return no(
            f"policy v{policy.version} already accepts up to {exc.max_variance_pct}%"
        )
    ids = [e.invoice_id for e in evidence]
    open_or_refused = await (
        await conn.execute(
            """SELECT id, status FROM rule_proposals
               WHERE vendor_id = %s AND flag = %s
                 AND (status = 'proposed' OR (status = 'rejected' AND evidence @> %s::int[]))
               ORDER BY id DESC LIMIT 1""",
            (vendor.id, flag, ids),
        )
    ).fetchone()
    if open_or_refused is not None:
        return no(
            f"proposal #{open_or_refused['id']} is already {open_or_refused['status']} "
            "for this evidence"
        )
    summary = (
        f"Accept price variance up to {pct}% over the PO for {vendor.name} "
        f"({vendor.code}); proposed after {', '.join(e.invoice_no for e in evidence)} "
        "were approved by people"
    )
    row = await (
        await conn.execute(
            """INSERT INTO rule_proposals (vendor_id, flag, max_variance_pct, evidence,
                                           base_version, summary, proposed_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (vendor.id, flag, pct, ids, policy.version, summary, proposed_by),
        )
    ).fetchone()
    assert row is not None
    await conn.execute(
        """INSERT INTO audit_log (invoice_id, actor, actor_kind, action, reason, details)
           VALUES (%s, %s, 'agent', 'rule_proposed', %s, %s)""",
        (
            invoice_id,
            proposed_by,
            summary,
            json.dumps({"proposal_id": row["id"], "evidence": ids}),
        ),
    )
    return {
        "proposed": True,
        "reason": summary,
        "proposal": await describe(conn, row["id"]),
    }


async def set_thread(
    conn: AsyncConnection, proposal_id: int, room_id: str | None, thread_id: str
) -> None:
    await conn.execute(
        "UPDATE rule_proposals SET switch_room_id = %s, switch_thread_id = %s WHERE id = %s",
        (room_id, thread_id, proposal_id),
    )


async def for_thread(conn: AsyncConnection, thread_id: str) -> dict[str, Any] | None:
    return await (
        await conn.execute(
            "SELECT id, status FROM rule_proposals WHERE switch_thread_id = %s",
            (thread_id,),
        )
    ).fetchone()


async def decide(
    conn: AsyncConnection,
    proposal_id: int,
    username: str,
    decision: str,
    reason: str,
    switch_message_id: str | None = None,
) -> dict[str, Any]:
    """Record the owner's decision on a proposal. Approval writes the new rule version.

    Run inside a transaction. Only someone with the owner role may decide; the
    new version is the active policy plus the exception, so any change made
    since the proposal was raised is kept.
    """
    decision = decision.strip().lower()
    if decision not in ("approve", "reject"):
        raise ProposalRefused(f"decision must be approve or reject, got {decision!r}")
    if len(reason.strip()) < 3:
        raise ProposalRefused("a rule decision needs a reason")
    p = await (
        await conn.execute(
            "SELECT * FROM rule_proposals WHERE id = %s FOR UPDATE", (proposal_id,)
        )
    ).fetchone()
    if p is None:
        raise LookupError(f"rule proposal {proposal_id} not found")
    if p["status"] != "proposed":
        raise ProposalRefused(f"proposal #{proposal_id} is already {p['status']}")
    who = await repo.person(conn, username)
    if who is None or who["role"] != "owner":
        name = who["display_name"] if who else username
        raise ProposalRefused(f"only the owner can decide a rule change, not {name}")

    new_version = None
    if decision == "approve":
        active = await (
            await conn.execute(
                "SELECT version, policy FROM rules WHERE active FOR UPDATE"
            )
        ).fetchone()
        assert active is not None
        vendor = await repo.get_vendor(conn, p["vendor_id"])
        policy = dict(active["policy"])
        exceptions = [
            e
            for e in policy.get("vendor_exceptions", [])
            if not (e["vendor_code"] == vendor.code and e["flag"] == p["flag"])
        ]
        invoice_nos = [
            r["invoice_no"]
            for r in await (
                await conn.execute(
                    "SELECT invoice_no FROM invoices WHERE id = ANY(%s) ORDER BY id",
                    (p["evidence"],),
                )
            ).fetchall()
        ]
        exceptions.append(
            {
                "vendor_code": vendor.code,
                "flag": p["flag"],
                "max_variance_pct": str(p["max_variance_pct"]),
                "proposal_id": proposal_id,
                "evidence": invoice_nos,
                "approved_by": who["username"],
            }
        )
        policy["vendor_exceptions"] = exceptions
        new_version = (
            await (await conn.execute("SELECT max(version) AS v FROM rules")).fetchone()
        )["v"] + 1
        await conn.execute("UPDATE rules SET active = false WHERE active")
        await conn.execute(
            """INSERT INTO rules (version, policy, change_summary, proposed_by, approved_by, active)
               VALUES (%s, %s, %s, %s, %s, true)""",
            (
                new_version,
                json.dumps(policy),
                p["summary"] + f" (proposal #{proposal_id}: {reason.strip()})",
                p["proposed_by"],
                who["username"],
            ),
        )
        await conn.execute(
            """INSERT INTO vendor_knowledge (vendor_id, kind, content)
               VALUES (%s, 'pattern', %s)""",
            (
                vendor.id,
                f"Policy v{new_version}: price variance up to {p['max_variance_pct']}% "
                f"over the PO is accepted for {vendor.name}, approved by "
                f"{who['display_name']} ({reason.strip()}), after "
                f"{', '.join(invoice_nos)}.",
            ),
        )

    status = "approved" if decision == "approve" else "rejected"
    await conn.execute(
        """UPDATE rule_proposals SET status = %s, decided_by = %s, decision_reason = %s,
               new_version = %s, switch_message_id = %s, decided_at = now()
           WHERE id = %s""",
        (
            status,
            who["username"],
            reason.strip(),
            new_version,
            switch_message_id,
            proposal_id,
        ),
    )
    await conn.execute(
        """INSERT INTO audit_log (actor, actor_kind, action, reason, details)
           VALUES (%s, 'human', %s, %s, %s)""",
        (
            who["username"],
            f"rule_{status}",
            reason.strip(),
            json.dumps({"proposal_id": proposal_id, "new_version": new_version}),
        ),
    )
    return {
        "proposal_id": proposal_id,
        "status": status,
        "decided_by": who["username"],
        "new_version": new_version,
        "summary": p["summary"],
    }


def decision_message(res: dict[str, Any]) -> str:
    if res["status"] == "approved":
        return (
            f"Recorded. Policy v{res['new_version']} is now active: {res['summary']}. "
            "The previous version is kept, and every past decision still shows the "
            "version it was made under."
        )
    return (
        f"Recorded: {res['decided_by']} rejected proposal #{res['proposal_id']}. "
        "The policy is unchanged, and I won't propose it again unless new approvals "
        "add to the pattern."
    )
