"""What each LLM agent must return. Every answer carries `confidence` and `reasons`.

Amounts are strings on purpose: the model transcribes what the document says
("73,632.00") and Khata parses it into a Decimal. The model never does arithmetic.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, field_validator

from khata.models import InvoiceData, InvoiceLine
from khata.money import money


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def parse_amount(text: str | None) -> Decimal:
    """'Rs. 73,632.00' -> Decimal('73632.00'). Raises ValueError on anything else."""
    if text is None:
        raise ValueError("missing amount")
    cleaned = re.sub(r"(?i)rs\.?|inr|₹|,|\s", "", str(text))
    if cleaned in {"", "-"}:
        return money("0")
    try:
        return money(cleaned)
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not an amount: {text!r}") from e


QTY_WITH_UNIT = re.compile(
    r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z][A-Za-z.]{0,11})?\s*$"
)


def parse_qty(text: str, unit: str | None) -> tuple[Decimal, str | None]:
    """'300' or '5 nos' -> (qty, unit). A reader sometimes writes the unit into the
    quantity; anything other than a number and an optional short word is refused."""
    m = QTY_WITH_UNIT.match(str(text))
    if m is None:
        raise ValueError(f"not a quantity: {text!r}")
    qty = Decimal(m.group(1).replace(",", ""))
    return qty, unit or (m.group(2).rstrip(".") if m.group(2) else None)


def parse_date(text: str) -> date:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {text!r}")


class ExtractedLine(BaseModel):
    description: str
    hsn: str | None
    qty: str
    unit: str | None
    rate: str
    gst_rate: str | None
    amount: str


class Extraction(BaseModel):
    """One model's reading of an invoice, transcribed exactly as printed."""

    vendor_name: str
    supplier_gstin: str
    buyer_gstin: str | None
    invoice_no: str
    invoice_date: str
    po_number: str | None
    taxable_value: str
    cgst: str
    sgst: str
    igst: str
    total: str
    bank_account: str | None
    ifsc: str | None
    lines: list[ExtractedLine]
    instructions_in_document: list[str]
    confidence: float
    reasons: list[str]

    @field_validator("confidence")
    @classmethod
    def _in_range(cls, v: float) -> float:
        return _clamp(v)

    def to_invoice_data(self) -> InvoiceData:
        """Parse the transcription into typed data. Raises ValueError if it doesn't parse."""

        def opt(s: str | None) -> str | None:
            s = (s or "").strip()
            return None if s.lower() in {"", "null", "none", "n/a", "-"} else s

        return InvoiceData(
            vendor_name=self.vendor_name.strip(),
            supplier_gstin=re.sub(r"\s", "", self.supplier_gstin).upper(),
            buyer_gstin=(
                re.sub(r"\s", "", b).upper() if (b := opt(self.buyer_gstin)) else None
            ),
            invoice_no=self.invoice_no.strip(),
            invoice_date=parse_date(self.invoice_date),
            po_number=opt(self.po_number),
            taxable_value=parse_amount(self.taxable_value),
            cgst=parse_amount(self.cgst or "0"),
            sgst=parse_amount(self.sgst or "0"),
            igst=parse_amount(self.igst or "0"),
            total=parse_amount(self.total),
            bank_account=(
                re.sub(r"\D", "", a) or None if (a := opt(self.bank_account)) else None
            ),
            ifsc=re.sub(r"\s", "", i).upper() if (i := opt(self.ifsc)) else None,
            lines=[
                InvoiceLine(
                    description=ln.description.strip(),
                    hsn=opt(ln.hsn),
                    qty=(q := parse_qty(ln.qty, opt(ln.unit)))[0],
                    unit=q[1],
                    rate=parse_amount(ln.rate),
                    gst_rate=(
                        Decimal(re.sub(r"[%\s]", "", g))
                        if (g := opt(ln.gst_rate))
                        else None
                    ),
                    amount=parse_amount(ln.amount),
                )
                for ln in self.lines
            ],
        )


class Precedent(BaseModel):
    invoice_no: str
    what_was_decided: str
    why_relevant: str


class AnalystNote(BaseModel):
    """The Analyst's explanation for the approver. Advice only."""

    headline: str
    explanation: str
    precedents: list[Precedent]
    questions_for_approver: list[str]
    suggested_action: Literal["approve", "reject", "investigate"]
    confidence: float
    reasons: list[str]

    @field_validator("confidence")
    @classmethod
    def _in_range(cls, v: float) -> float:
        return _clamp(v)


class AuditVerdict(BaseModel):
    """The Auditor's check of the Analyst's note against the recorded facts."""

    consistent: bool
    issues: list[str]
    confidence: float
    reasons: list[str]

    @field_validator("confidence")
    @classmethod
    def _in_range(cls, v: float) -> float:
        return _clamp(v)
