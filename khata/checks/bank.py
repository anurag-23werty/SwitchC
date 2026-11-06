"""Bank-detail change detection. A change is always a hard stop for the owner."""

from __future__ import annotations

from khata.models import CheckReport, Flag, InvoiceData, Vendor


def _norm(value: str | None) -> str:
    return "".join((value or "").upper().split())


def mask(account: str) -> str:
    return "x" * max(len(account) - 4, 0) + account[-4:]


def check_bank_change(inv: InvoiceData, vendor: Vendor) -> CheckReport:
    """Compare the bank details printed on the invoice with the vendor master.

    Khata only ever pays to the vendor master. An invoice printing different
    details is how invoice-redirection fraud starts, so it stops the invoice
    and asks the owner to call the phone number already on file, never a
    number printed on the invoice.
    """
    r = CheckReport(check="check_bank_change")
    if not inv.bank_account and not inv.ifsc:
        r.add(None, "No bank details printed; payment would go to the vendor master")
        return r
    account_changed = _norm(inv.bank_account) != _norm(vendor.bank_account)
    ifsc_changed = bool(inv.ifsc) and _norm(inv.ifsc) != _norm(vendor.ifsc)
    if account_changed or ifsc_changed:
        r.add(
            Flag.BANK_CHANGE,
            f"Invoice asks for payment to A/c {mask(_norm(inv.bank_account))} "
            f"(IFSC {inv.ifsc}), but {vendor.name}'s account on file is "
            f"{mask(vendor.bank_account)} (IFSC {vendor.ifsc})",
            invoice_account=_norm(inv.bank_account),
            invoice_ifsc=_norm(inv.ifsc),
            master_account=vendor.bank_account,
            master_ifsc=vendor.ifsc,
            callback_phone=vendor.phone,
        )
    else:
        r.add(None, "Bank details match the vendor master")
    return r
