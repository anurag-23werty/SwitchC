"""Typed data shared by checks, tools, the graph and the database layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Flag(StrEnum):
    """Exception codes. Each maps to exactly one outcome in khata/approval.py."""

    # Vendor must reissue: rejected outright.
    GSTIN_INVALID = "GSTIN_INVALID"
    GST_TYPE_MISMATCH = "GST_TYPE_MISMATCH"
    GST_SPLIT_UNEQUAL = "GST_SPLIT_UNEQUAL"
    ARITHMETIC = "ARITHMETIC"
    DUPLICATE = "DUPLICATE"
    BUYER_GSTIN_MISMATCH = "BUYER_GSTIN_MISMATCH"
    # A person must look before anything else happens: on hold.
    BANK_CHANGE = "BANK_CHANGE"
    INJECTION = "INJECTION"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    PO_VENDOR_MISMATCH = "PO_VENDOR_MISMATCH"
    VENDOR_UNKNOWN = "VENDOR_UNKNOWN"
    GSTIN_MISMATCH = "GSTIN_MISMATCH"
    EXTRACTION_DISAGREEMENT = "EXTRACTION_DISAGREEMENT"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    # Fine to continue, but a person approves: routed to the finance manager.
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QTY_VARIANCE = "QTY_VARIANCE"
    FIRST_TIME_VENDOR = "FIRST_TIME_VENDOR"


class State(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    MATCHED = "MATCHED"
    REVIEWED = "REVIEWED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    PAYMENT_DRAFTED = "PAYMENT_DRAFTED"
    ON_HOLD = "ON_HOLD"
    REJECTED = "REJECTED"


class Finding(BaseModel):
    """One thing a check noticed. `flag` is None for informational findings."""

    check: str
    flag: Flag | None
    message: str
    details: dict = Field(default_factory=dict)


class CheckReport(BaseModel):
    check: str
    findings: list[Finding] = Field(default_factory=list)

    @property
    def flags(self) -> list[Flag]:
        return [f.flag for f in self.findings if f.flag is not None]

    def add(self, flag: Flag | None, message: str, **details) -> None:
        self.findings.append(
            Finding(check=self.check, flag=flag, message=message, details=details)
        )


# --- The invoice as read from the document -----------------------------------


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    hsn: str | None = None
    qty: Decimal
    unit: str | None = None
    rate: Decimal
    gst_rate: Decimal | None = None
    amount: Decimal


class InvoiceData(BaseModel):
    """What an invoice says. Untrusted: it comes from the document."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: str
    supplier_gstin: str
    buyer_gstin: str | None = None
    invoice_no: str
    invoice_date: date
    po_number: str | None = None
    taxable_value: Decimal
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")
    total: Decimal
    bank_account: str | None = None
    ifsc: str | None = None
    lines: list[InvoiceLine]


# --- Masters, as loaded from the database ------------------------------------


class Company(BaseModel):
    name: str
    gstin: str
    state_code: str


class Vendor(BaseModel):
    id: int
    code: str
    name: str
    gstin: str
    state_code: str
    phone: str
    email: str
    bank_name: str
    bank_account: str
    ifsc: str


class POLine(BaseModel):
    id: int
    line_no: int
    description: str
    hsn: str
    qty: Decimal
    rate: Decimal
    gst_rate: Decimal
    qty_received: Decimal


class PurchaseOrder(BaseModel):
    id: int
    po_number: str
    vendor_id: int
    lines: list[POLine]


class PriorInvoice(BaseModel):
    id: int
    invoice_no: str
    invoice_date: date
    total: Decimal
    state: State


class VendorException(BaseModel):
    """A variance one vendor is allowed, written into the policy by an approved
    Policy Steward proposal. Only PRICE_VARIANCE is ever eligible."""

    vendor_code: str
    flag: str
    max_variance_pct: Decimal
    proposal_id: int | None = None
    evidence: list[str] = Field(default_factory=list)  # invoice numbers it came from
    approved_by: str | None = None


class Policy(BaseModel):
    """The approval policy. Loaded from the active row of the `rules` table."""

    version: int
    auto_approve_below: Decimal
    manager_up_to: Decimal
    variance_pct: Decimal
    duplicate_window_days: int
    arithmetic_tolerance: Decimal
    approvers: dict[str, str]  # role -> username
    min_extraction_confidence: Decimal = Decimal("0.70")
    injection_score_threshold: Decimal = Decimal("0.90")
    po_required: bool = True
    grn_required: bool = True
    vendor_exceptions: list[VendorException] = Field(default_factory=list)
    # Policy Steward: how many matching human approvals make a pattern, the
    # flags it may ever propose relaxing, and the largest variance it may propose.
    steward_min_overrides: int = 2
    steward_eligible_flags: list[str] = Field(
        default_factory=lambda: ["PRICE_VARIANCE"]
    )
    steward_max_variance_pct: Decimal = Decimal("10.0")

    def exception_for(
        self, vendor_code: str | None, flag: str
    ) -> VendorException | None:
        return next(
            (
                e
                for e in self.vendor_exceptions
                if vendor_code and e.vendor_code == vendor_code and e.flag == flag
            ),
            None,
        )


class RouteDecision(BaseModel):
    """What the approval policy says to do with an invoice. Computed, never guessed."""

    gate_state: State  # APPROVED (auto) | AWAITING_APPROVAL | ON_HOLD | REJECTED
    route: str  # auto | manager | manager_then_owner | owner | none
    steps: list[str]  # approval steps in order: "manager", "owner"
    approvers: list[str]  # usernames for those steps
    reasons: list[str]
    hard_stop: bool = False
    callback_instruction: str | None = None
    rule_version: int
