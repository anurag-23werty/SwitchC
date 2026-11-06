"""The approval policy, applied. Flags + total + policy in; a route out. No LLM involved.

Precedence, strongest first:
1. Anything the vendor must fix (bad GSTIN, wrong GST, bad arithmetic, duplicate) -> REJECTED.
2. A bank-detail change -> ON_HOLD for the owner, with a callback instruction. Hard stop.
3. Anything a person must look at first (injection, possible duplicate, unknown PO,
   model disagreement...) -> ON_HOLD for the finance manager.
4. Otherwise the amount and the approval flags decide: auto-approve, manager,
   or manager then owner.
"""

from __future__ import annotations

from decimal import Decimal

from khata.models import Flag, Policy, RouteDecision, State, Vendor

REJECT = {
    Flag.GSTIN_INVALID: "supplier GSTIN is invalid",
    Flag.GST_TYPE_MISMATCH: "wrong type of GST charged",
    Flag.GST_SPLIT_UNEQUAL: "CGST and SGST are not equal halves",
    Flag.ARITHMETIC: "the invoice does not add up",
    Flag.DUPLICATE: "this invoice number is already in the books",
    Flag.BUYER_GSTIN_MISMATCH: "billed to someone else's GSTIN",
}
HOLD_FOR_MANAGER = {
    Flag.INJECTION: "the document contains instructions aimed at our agents",
    Flag.POSSIBLE_DUPLICATE: "same amount as a recent invoice from this vendor",
    Flag.PO_NOT_FOUND: "the PO it quotes is not in our books",
    Flag.PO_VENDOR_MISMATCH: "the PO belongs to a different vendor",
    Flag.VENDOR_UNKNOWN: "the vendor is not in the vendor master",
    Flag.GSTIN_MISMATCH: "the GSTIN differs from the vendor master",
    Flag.EXTRACTION_DISAGREEMENT: "the two models read the invoice differently",
    Flag.LOW_CONFIDENCE: "the invoice could not be read with confidence",
}
NEEDS_MANAGER = {
    Flag.PRICE_VARIANCE: "price is over the PO beyond tolerance",
    Flag.QTY_VARIANCE: "billed quantity is more than we received",
    Flag.FIRST_TIME_VENDOR: "first invoice from this vendor",
}


def _with_owner_above_limit(
    steps: list[str], total: Decimal, policy: Policy
) -> list[str]:
    return (
        steps + ["owner"]
        if total > policy.manager_up_to and "owner" not in steps
        else steps
    )


def route_approval(
    flags: list[Flag], total: Decimal, policy: Policy, vendor: Vendor | None
) -> RouteDecision:
    flagset = set(flags)

    def decide(
        gate: State, route: str, steps: list[str], reasons: list[str], **kw
    ) -> RouteDecision:
        role = {"manager": "finance_manager", "owner": "owner"}
        return RouteDecision(
            gate_state=gate,
            route=route,
            steps=steps,
            approvers=[policy.approvers[role[s]] for s in steps],
            reasons=reasons,
            rule_version=policy.version,
            **kw,
        )

    rejected = [msg for f, msg in REJECT.items() if f in flagset]
    if rejected:
        return decide(State.REJECTED, "none", [], [f"Rejected: {m}" for m in rejected])

    if Flag.BANK_CHANGE in flagset:
        phone = vendor.phone if vendor else "the number in the vendor master"
        return decide(
            State.ON_HOLD,
            "owner",
            ["owner"],
            ["Bank details differ from the vendor master: owner must verify"],
            hard_stop=True,
            callback_instruction=(
                f"Call {vendor.name if vendor else 'the vendor'} on {phone} (the number on "
                "file, not one printed on the invoice) and confirm the bank change before "
                "anything is paid."
            ),
        )

    held = [msg for f, msg in HOLD_FOR_MANAGER.items() if f in flagset]
    if held:
        steps = _with_owner_above_limit(["manager"], total, policy)
        route = "manager" if steps == ["manager"] else "manager_then_owner"
        return decide(State.ON_HOLD, route, steps, [f"On hold: {m}" for m in held])

    needs = [msg for f, msg in NEEDS_MANAGER.items() if f in flagset]
    if total > policy.manager_up_to:
        return decide(
            State.AWAITING_APPROVAL,
            "manager_then_owner",
            ["manager", "owner"],
            [f"Above Rs {policy.manager_up_to}: finance manager, then owner", *needs],
        )
    if needs or total >= policy.auto_approve_below:
        reasons = needs or [
            f"Rs {policy.auto_approve_below} to Rs {policy.manager_up_to}: finance manager"
        ]
        return decide(State.AWAITING_APPROVAL, "manager", ["manager"], reasons)
    return decide(
        State.APPROVED,
        "auto",
        [],
        [f"Clean match under Rs {policy.auto_approve_below}: auto-approved"],
    )
