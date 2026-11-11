"""What the agents say. Plain text built by code from recorded facts.

Phase 3 prints these from the CLI; Phase 4 posts them into the invoice's Switch
thread. The LLM's note is quoted as the Analyst's view; every number and route
in the message comes from the database, not the model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from khata.money import inr

STEP_LABEL = {"manager": "Finance manager", "owner": "Owner"}


def _amount(v: Any) -> str:
    return "₹" + inr(Decimal(str(v))) if v is not None else "?"


def extraction_line(inv: dict[str, Any], rec: dict[str, Any]) -> str:
    usable = [r for r in (rec["a"], rec["b"]) if r.get("fields") and not r.get("error")]
    who = ", ".join(f"{r['endpoint']} ({r['model']})" for r in usable)
    if rec["disagreements"]:
        agree = "Readers disagree: " + "; ".join(rec["notes"])
    elif len(usable) == 2:
        agree = f"Both readers agree ({who})."
    else:
        agree = f"Read by {who or 'no reader'} only."
    extra = (
        " " + " ".join(n for n in rec["notes"] if not rec["disagreements"])
        if rec["notes"]
        else ""
    )
    return (
        f"Read {inv['invoice_no']} from {inv['vendor_name'] or 'an unknown vendor'}, "
        f"total {_amount(inv['total'])}. {agree}{extra}"
    )


VERIFIER_CHECKS = ("three_way_match", "check_duplicate", "check_bank_change")


def checks_line(reports: dict[str, Any]) -> str:
    """The Verifier's own three checks (Intake reports GST and injection)."""
    flagged = [
        f"{name}: {', '.join(dict.fromkeys(reports[name]['flags']))}"
        for name in VERIFIER_CHECKS
        if name in reports and reports[name]["flags"]
    ]
    return (
        "PO/GRN match, duplicate and bank checks: "
        + ("; ".join(flagged) if flagged else "all passed")
        + "."
    )


def route_line(route: dict[str, Any]) -> str:
    state = route["state"]
    why = "; ".join(route["reasons"])
    if state == "APPROVED":
        return f"Auto-approved under policy v{route['rule_version']}: {why}."
    if state == "REJECTED":
        return f"Rejected under policy v{route['rule_version']}: {why}. The vendor must reissue."
    who = " then ".join(STEP_LABEL[s] for s in route["steps"])
    stop = " HARD STOP." if route.get("hard_stop") else ""
    return f"{state.replace('_', ' ').title()} for {who}.{stop} {why}."


def approval_message(req: dict[str, Any]) -> str:
    """The message a person answers with `approve <reason>` or `reject <reason>`."""
    out = [
        f"{req['vendor_name']} {req['invoice_no']} for {_amount(req['total'])} "
        f"needs {STEP_LABEL[req['next_step']].lower()} approval "
        f"(@{req['next_approver']})."
    ]
    if req.get("hard_stop"):
        out.append(f"HARD STOP: {req['callback_instruction']}")
    out.append("Flags: " + (", ".join(req["flags"]) or "none") + ".")
    note = req.get("analyst")
    if note:
        out.append(f"Analyst: {note['headline']} {note['explanation']}")
        for p in note["precedents"]:
            out.append(f"  Precedent {p['invoice_no']}: {p['what_was_decided']}")
        for q in note["questions_for_approver"]:
            out.append(f"  Check: {q}")
    audit = req.get("audit")
    if audit:
        out.append(
            "Auditor: agrees with the note."
            if audit["consistent"]
            else "Auditor disagrees: " + "; ".join(audit["issues"])
        )
    if req.get("error"):
        out.append(f"Not recorded: {req['error']}")
    out.append("Reply `approve <reason>` or `reject <reason>`.")
    return "\n".join(out)
