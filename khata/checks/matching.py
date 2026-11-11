"""Three-way match: invoice lines against the PO (price) and the GRN (quantity received)."""

from __future__ import annotations

from decimal import Decimal
from difflib import SequenceMatcher

from khata.models import (
    CheckReport,
    Flag,
    InvoiceData,
    InvoiceLine,
    POLine,
    PurchaseOrder,
)
from khata.money import money


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def pair_lines(
    lines: list[InvoiceLine], po_lines: list[POLine]
) -> list[tuple[InvoiceLine, POLine | None]]:
    """Pair each invoice line with at most one PO line.

    Same HSN first (closest description wins when several share it), then any
    PO line whose description is clearly the same thing. Unpaired invoice lines
    (freight, packing, "miscellaneous") are charges the PO never agreed to.
    """
    free = list(po_lines)
    pairs: list[tuple[InvoiceLine, POLine | None]] = []
    for line in lines:
        candidates = [p for p in free if line.hsn and p.hsn == line.hsn]
        if not candidates:
            candidates = [
                p for p in free if _similarity(line.description, p.description) >= 0.6
            ]
        best = max(
            candidates,
            key=lambda p: _similarity(line.description, p.description),
            default=None,
        )
        if best is not None:
            free.remove(best)
        pairs.append((line, best))
    return pairs


def _pct(over: Decimal, base: Decimal) -> Decimal:
    return (over / base * 100).quantize(Decimal("0.01")) if base else Decimal("0")


def three_way_match(
    inv: InvoiceData,
    po: PurchaseOrder | None,
    variance_pct: Decimal,
    po_required: bool = True,
    grn_required: bool = True,
) -> CheckReport:
    """Flag price variance and quantity variance above `variance_pct` percent.

    `po_required` / `grn_required` come from the policy: a business that doesn't
    raise POs isn't flagged for an invoice without one, and one that doesn't record
    goods receipts has billed quantities checked against the PO's ordered quantities.

    Price: what was billed vs what the PO price would give for the billed
    quantities, with any charge not on the PO counted as variance.
    Quantity: billed quantity vs quantity received on the GRN, per line.
    """
    r = CheckReport(check="three_way_match")
    if po is None:
        if po_required:
            r.add(
                Flag.PO_NOT_FOUND,
                f"PO {inv.po_number or '(none quoted)'} is not in our books",
                po_number=inv.po_number,
            )
        elif inv.po_number:
            r.add(
                None,
                f"Quotes PO {inv.po_number}, which isn't in Khata; POs aren't required here",
                po_number=inv.po_number,
            )
        else:
            r.add(None, "No PO quoted; POs aren't required here")
        return r

    agreed = Decimal("0")  # billed quantities at PO prices
    billed = Decimal("0")
    extras: list[dict] = []
    for line, po_line in pair_lines(inv.lines, po.lines):
        billed += line.amount
        if po_line is None:
            extras.append({"description": line.description, "amount": str(line.amount)})
            continue
        agreed += money(line.qty * po_line.rate)
        rate_over = _pct(line.rate - po_line.rate, po_line.rate)
        if rate_over > variance_pct:
            r.add(
                None,
                f"'{po_line.description}' billed at Rs {line.rate} vs PO Rs {po_line.rate} "
                f"(+{rate_over}%)",
                po_line=po_line.line_no,
            )
        if not grn_required and po_line.qty_received <= 0:
            # Nothing received on record and GRNs aren't required: hold the bill
            # against what was ordered instead.
            if line.qty > po_line.qty:
                qty_over = _pct(line.qty - po_line.qty, po_line.qty)
                if qty_over > variance_pct:
                    r.add(
                        Flag.QTY_VARIANCE,
                        f"'{po_line.description}': billed {line.qty}, ordered {po_line.qty} "
                        f"(+{qty_over}%)",
                        po_line=po_line.line_no,
                        billed=str(line.qty),
                        ordered=str(po_line.qty),
                        variance_pct=str(qty_over),
                    )
        elif po_line.qty_received <= 0 or line.qty > po_line.qty_received:
            qty_over = (
                _pct(line.qty - po_line.qty_received, po_line.qty_received)
                if po_line.qty_received
                else Decimal("100")
            )
            if qty_over > variance_pct:
                r.add(
                    Flag.QTY_VARIANCE,
                    f"'{po_line.description}': billed {line.qty}, received {po_line.qty_received} "
                    f"(+{qty_over}%)",
                    po_line=po_line.line_no,
                    billed=str(line.qty),
                    received=str(po_line.qty_received),
                    variance_pct=str(qty_over),
                )

    for extra in extras:
        r.add(
            None, f"Charge not on the PO: '{extra['description']}' Rs {extra['amount']}"
        )
    over = _pct(billed - agreed, agreed)
    if over > variance_pct:
        r.add(
            Flag.PRICE_VARIANCE,
            f"Billed Rs {billed} against Rs {agreed} at PO prices: +{over}% "
            f"(limit {variance_pct}%)",
            billed=str(billed),
            agreed=str(agreed),
            variance_pct=str(over),
            extras=extras,
        )
    elif billed != agreed:
        r.add(
            None,
            f"Billed Rs {billed} vs Rs {agreed} at PO prices: {over}%, within {variance_pct}%",
        )
    if not r.flags:
        r.add(None, f"Matches {po.po_number} and its goods receipt")
    return r
