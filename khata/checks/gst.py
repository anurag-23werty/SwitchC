"""GSTIN rules: format, checksum and state code. Pure functions, no I/O."""

from __future__ import annotations

import re
from decimal import Decimal

from khata.models import CheckReport, Company, Flag, InvoiceData
from khata.money import money

GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATE_NAMES = {
    "07": "Delhi",
    "27": "Maharashtra",
    "29": "Karnataka",
    "33": "Tamil Nadu",
}


def gstin_check_char(first14: str) -> str:
    """The 15th character for the first 14.

    Map 0-9A-Z to 0..35. For index i (0-based), factor = 1 if i is even else 2;
    p = value * factor; sum += p // 36 + p % 36. Check = (36 - sum % 36) % 36.
    """
    if len(first14) != 14:
        raise ValueError(f"need 14 characters, got {len(first14)}")
    total = 0
    for i, ch in enumerate(first14):
        p = _CHARSET.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return _CHARSET[(36 - total % 36) % 36]


def is_valid_format(gstin: str) -> bool:
    return bool(GSTIN_PATTERN.fullmatch(gstin))


def is_valid_gstin(gstin: str) -> bool:
    """Format and checksum both hold."""
    return is_valid_format(gstin) and gstin_check_char(gstin[:14]) == gstin[14]


def make_gstin(state_code: str, pan: str, entity: str = "1") -> str:
    """Build a valid GSTIN from a state code and PAN (used for demo data)."""
    first14 = f"{state_code}{pan}{entity}Z"
    gstin = first14 + gstin_check_char(first14)
    if not is_valid_gstin(gstin):
        raise ValueError(f"built an invalid GSTIN {gstin}")
    return gstin


def state_code(gstin: str) -> str:
    return gstin[:2]


# --- Invoice-level GST and arithmetic validation ------------------------------


def validate_gst(inv: InvoiceData, company: Company, tolerance: Decimal) -> CheckReport:
    """GSTINs, the type of GST charged, the CGST/SGST split, and the arithmetic.

    Taxes are recomputed per line from qty x rate x GST rate; amounts may be off
    by at most `tolerance` rupees (rounding) before they count as wrong.
    """
    r = CheckReport(check="validate_gst")

    if not is_valid_format(inv.supplier_gstin):
        r.add(
            Flag.GSTIN_INVALID,
            f"Supplier GSTIN {inv.supplier_gstin!r} is not in GSTIN format",
        )
    elif not is_valid_gstin(inv.supplier_gstin):
        expected = gstin_check_char(inv.supplier_gstin[:14])
        r.add(
            Flag.GSTIN_INVALID,
            f"Supplier GSTIN {inv.supplier_gstin} fails its checksum "
            f"(15th character should be {expected})",
            expected_check_char=expected,
        )
    if inv.buyer_gstin and inv.buyer_gstin != company.gstin:
        r.add(
            Flag.BUYER_GSTIN_MISMATCH,
            f"Billed to GSTIN {inv.buyer_gstin}, but ours is {company.gstin}",
        )

    supplier_state = inv.supplier_gstin[:2]
    intra = supplier_state == company.state_code
    zero = Decimal("0")
    if intra and inv.igst > zero:
        r.add(
            Flag.GST_TYPE_MISMATCH,
            f"Supplier and buyer are both in state {supplier_state}: an intra-state "
            f"supply takes CGST + SGST, but IGST of Rs {inv.igst} was charged",
            supplier_state=supplier_state,
            buyer_state=company.state_code,
        )
    if not intra and (inv.cgst > zero or inv.sgst > zero):
        r.add(
            Flag.GST_TYPE_MISMATCH,
            f"Supplier state {supplier_state} differs from ours ({company.state_code}): "
            f"an inter-state supply takes IGST, but CGST/SGST was charged",
            supplier_state=supplier_state,
            buyer_state=company.state_code,
        )
    if (inv.cgst > zero or inv.sgst > zero) and abs(inv.cgst - inv.sgst) > tolerance:
        r.add(
            Flag.GST_SPLIT_UNEQUAL,
            f"CGST (Rs {inv.cgst}) and SGST (Rs {inv.sgst}) must be equal halves",
        )

    # Arithmetic: lines, taxable value, tax, total.
    line_sum = Decimal("0")
    expected_tax = Decimal("0")
    for n, line in enumerate(inv.lines, start=1):
        computed = money(line.qty * line.rate)
        if abs(computed - line.amount) > tolerance:
            r.add(
                Flag.ARITHMETIC,
                f"Line {n}: {line.qty} x {line.rate} = {computed}, but it says {line.amount}",
            )
        line_sum += line.amount
        if line.gst_rate is not None:
            expected_tax += money(line.amount * line.gst_rate / 100)
    if abs(line_sum - inv.taxable_value) > tolerance:
        r.add(
            Flag.ARITHMETIC,
            f"Lines add up to Rs {line_sum}, but taxable value says Rs {inv.taxable_value}",
        )
    charged_tax = inv.cgst + inv.sgst + inv.igst
    if (
        all(line.gst_rate is not None for line in inv.lines)
        and abs(expected_tax - charged_tax) > tolerance
    ):
        r.add(
            Flag.ARITHMETIC,
            f"Tax at the line GST rates is Rs {expected_tax}, but Rs {charged_tax} was charged",
        )
    computed_total = inv.taxable_value + charged_tax
    if abs(computed_total - inv.total) > tolerance:
        r.add(
            Flag.ARITHMETIC,
            f"Taxable Rs {inv.taxable_value} + tax Rs {charged_tax} = Rs {computed_total}, "
            f"but the invoice total is Rs {inv.total}",
        )
    if not r.flags:
        kind = "CGST + SGST" if intra else "IGST"
        r.add(None, f"GSTINs valid, {kind} correctly charged, arithmetic adds up")
    return r
