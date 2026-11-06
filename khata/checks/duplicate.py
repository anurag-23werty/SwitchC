"""Duplicate detection against invoices already in the books."""

from __future__ import annotations

from khata.models import CheckReport, Flag, InvoiceData, PriorInvoice, State


def _norm(invoice_no: str) -> str:
    return "".join(invoice_no.upper().split())


def check_duplicate(
    inv: InvoiceData, prior: list[PriorInvoice], window_days: int
) -> CheckReport:
    """Same vendor + same invoice number: duplicate. Same vendor + same total
    within `window_days`: possible duplicate.

    `prior` is the vendor's other invoices. Rejected ones are ignored, so a
    vendor may reissue a rejected invoice under the same number.
    """
    r = CheckReport(check="check_duplicate")
    live = [p for p in prior if p.state != State.REJECTED]
    for p in live:
        if _norm(p.invoice_no) == _norm(inv.invoice_no):
            r.add(
                Flag.DUPLICATE,
                f"Invoice {inv.invoice_no} is already in the books (#{p.id}, {p.state.value}, "
                f"Rs {p.total}, dated {p.invoice_date})",
                duplicate_of=p.id,
            )
            return r
    for p in live:
        days = abs((inv.invoice_date - p.invoice_date).days)
        if p.total == inv.total and days <= window_days:
            r.add(
                Flag.POSSIBLE_DUPLICATE,
                f"Same total Rs {inv.total} as {p.invoice_no} (#{p.id}) {days} day(s) apart",
                similar_to=p.id,
            )
    if not r.flags:
        r.add(
            None, f"No earlier invoice with this number or total ({len(live)} checked)"
        )
    return r
