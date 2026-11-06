"""Analyst and Auditor: explanations for the approver, from two different providers.

Both read facts through the ToolGateway as their own agent (get_invoice,
search_vendor_knowledge). Neither decides anything: the route is already set by
policy, and a person approves. Code then checks their output: a precedent the
Analyst cites must be one it was actually shown, and the Auditor must run on a
different provider (or at least a different model) from the Analyst.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from khata.agents.prompts import ANALYST, AUDITOR, facts_block
from khata.agents.schemas import AnalystNote, AuditVerdict
from khata.llm import LLM, LLMUnavailable
from khata.policy import ToolGateway

logger = logging.getLogger(__name__)

ANALYST_CHAIN = ("groq", "groq-secondary")
AUDITOR_CHAIN = ("openrouter", "groq-secondary", "groq")

# What each check said, from the audit trail (the check's own messages).
_CHECK_ACTIONS = {
    "validate_gst",
    "detect_injection",
    "three_way_match",
    "check_duplicate",
    "check_bank_change",
}


async def gather_facts(gw: ToolGateway, agent: str, invoice_id: int) -> dict[str, Any]:
    inv = await gw.call(agent, "get_invoice", invoice_id=invoice_id)
    findings = [
        {"check": h["action"], "said": h["reason"]}
        for h in inv["history"]
        if h["action"] in _CHECK_ACTIONS and h["reason"]
    ]
    route = next(
        (
            {"to": h["to_state"], "why": h["reason"]}
            for h in reversed(inv["history"])
            if h["action"] == "transition"
            and h["to_state"]
            in {"AWAITING_APPROVAL", "ON_HOLD", "REJECTED", "APPROVED"}
        ),
        None,
    )
    knowledge: list[dict[str, Any]] = []
    if inv.get("vendor_code"):
        query = " ".join([*inv["flags"], *(ln["description"] for ln in inv["lines"])])
        kn = await gw.call(
            agent,
            "search_vendor_knowledge",
            vendor_code=inv["vendor_code"],
            query=query,
        )
        knowledge = [
            {"kind": k["kind"], "invoice_no": k["invoice_no"], "text": k["text"]}
            for k in kn["items"]
            if k.get("invoice_id") != invoice_id
        ]
    return {
        "invoice": {
            k: inv[k]
            for k in (
                "invoice_no",
                "invoice_date",
                "vendor_name",
                "vendor_code",
                "po_number",
                "taxable_value",
                "cgst",
                "sgst",
                "igst",
                "total",
                "state",
            )
        },
        "lines": [
            {k: ln[k] for k in ("description", "hsn", "qty", "rate", "amount")}
            for ln in inv["lines"]
        ],
        "flags": inv["flags"],
        "check_findings": findings,
        "policy_outcome": route,
        "knowledge": knowledge,
    }


@dataclass
class Review:
    note: AnalystNote | None = None
    analyst_model: str | None = None
    analyst_provider: str | None = None
    verdict: AuditVerdict | None = None
    auditor_model: str | None = None
    auditor_provider: str | None = None
    corrections: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def analyse(
    llm: LLM, gw: ToolGateway, invoice_id: int
) -> tuple[Review, dict[str, Any]]:
    """Analyst note (advice), checked by code. Never raises on an LLM outage."""
    facts = await gather_facts(gw, "analyst", invoice_id)
    review = Review()
    try:
        res = await llm.structured(
            AnalystNote,
            system=ANALYST,
            user="Write the note for this invoice.\n\n" + facts_block(facts),
            chain=ANALYST_CHAIN,
            agent="analyst",
            invoice_id=invoice_id,
        )
    except LLMUnavailable as e:
        review.errors.append(f"analyst unavailable: {e}"[:300])
        return review, facts
    note = res.value
    known = {k["invoice_no"] for k in facts["knowledge"] if k["invoice_no"]}
    kept = [p for p in note.precedents if p.invoice_no in known]
    for p in note.precedents:
        if p.invoice_no not in known:
            review.corrections.append(
                f"Removed precedent {p.invoice_no}: not among the decisions on record."
            )
    note.precedents = kept
    review.note = note
    review.analyst_model, review.analyst_provider = res.model, res.provider
    return review, facts


async def audit_review(
    llm: LLM, gw: ToolGateway, invoice_id: int, review: Review
) -> Review:
    """Independent check of the Analyst's note on another provider."""
    if review.note is None:
        return review
    facts = await gather_facts(gw, "auditor", invoice_id)
    chain = [
        n
        for n in AUDITOR_CHAIN
        if llm.endpoints.get(n) and llm.endpoints[n].model != review.analyst_model
    ]
    try:
        res = await llm.structured(
            AuditVerdict,
            system=AUDITOR,
            user=(
                "Facts on record:\n"
                + facts_block(facts)
                + "\n\nThe Analyst's note:\n"
                + facts_block(review.note.model_dump(mode="json"))
            ),
            chain=chain,
            agent="auditor",
            invoice_id=invoice_id,
        )
    except LLMUnavailable as e:
        review.errors.append(f"auditor unavailable: {e}"[:300])
        return review
    review.verdict = res.value
    review.auditor_model, review.auditor_provider = res.model, res.provider
    if res.provider == review.analyst_provider:
        review.errors.append(
            f"Auditor ran on the same provider as the Analyst ({res.provider}); "
            "second opinion is weaker than intended."
        )
    return review
